---
title: External Participants
sidebar_label: External participants
---

# External Participants

A **participant** is any agent other than Hermes that speaks into a Hermes
session: another CLI agent driven by a plugin, a remote worker, a bridged
assistant. The participant seam is the supported way to put such a voice into a
session's transcript — it persists an attributed message row, keeps the live
gateway history in sync, and emits the stream events every Hermes surface
renders from.

The seam is generic. Core knows nothing about which agent is speaking: a plugin
supplies its own roster and owns the turns it publishes.

```python
from tui_gateway import participants

participants.register_participants(
    session_id,                     # gateway/UI session id
    "my-plugin",
    [{
        "id": "peer:default",
        "handle": "peer",
        "display_name": "Peer Agent",
        "adapter_id": "peer-stdio",
        "status": "ready",
        "capabilities": {"text": True, "streaming": True},
    }],
)

turn = "pturn-8f21"
participants.begin_participant_message(session_id, "my-plugin", "peer:default", turn)
participants.append_participant_delta(session_id, "my-plugin", turn, "Looks fine ")
participants.append_participant_delta(session_id, "my-plugin", turn, "to me.")
participants.complete_participant_message(session_id, "my-plugin", turn)
```

## Session ids

Every function takes the **gateway/UI session id** — the same identifier that
appears in event frames as `params.session_id` and that `session.*` RPCs
accept. It is not the durable SQLite session id; core resolves that internally.

A caller running inside a Hermes tool call (rather than handling a UI request)
can ask for the session it is running in:

```python
session_id = participants.resolve_publish_session_id()        # from context
session_id = participants.resolve_publish_session_id(explicit) # or validate one
```

The resolver reads the live UI session bound to the current turn and validates
it against the live session table. It never falls back to `HERMES_SESSION_ID`:
that names a database row, so routing by it would publish into whichever
session happens to share it. When nothing resolves, it raises
`UnknownSessionError` — publish nowhere rather than somewhere wrong.

## What gets persisted

No schema change and no new message role. Two `display_kind` values on the
existing `messages` columns carry everything:

### A participant's reply

| column | value |
| --- | --- |
| `role` | `assistant` |
| `display_kind` | `participant_message` |
| `content` | the final text (empty while streaming) |
| `display_metadata` | see below |

```json
{
  "participant": {
    "id": "peer:default",
    "handle": "peer",
    "display_name": "Peer Agent",
    "plugin_id": "my-plugin",
    "adapter_id": "peer-stdio"
  },
  "participant_turn_id": "pturn-8f21",
  "status": "streaming | completed | failed | interrupted",
  "error": "present only when status is failed"
}
```

The row is `role: assistant` because that is how it **renders** — an
assistant-style bubble with an attribution header. It is not replayed to the
model as one; see [Model context](#model-context).

### A human message addressed to a participant

| column | value |
| --- | --- |
| `role` | `user` |
| `display_kind` | `participant_directed` |
| `content` | the human text as typed, mentions included |
| `display_metadata` | `{"mentions": ["peer"], "plugin_id": "my-plugin"}` |

This stays a genuine user message — the human really typed it — so it remains
part of Hermes's context. The `display_kind` only marks that it was addressed
elsewhere, which keeps it out of undo/rewind targeting and out of anything that
asks "what was the last thing the user asked me?".

## Model context

Hermes must be able to reason about what a participant said — "critique the
reply above" has to work — but a peer agent must never borrow Hermes's own
voice. So participant rows are **projected, not dropped**. Immediately before
the outgoing request is built, each `participant_message` row is replaced (in
the request copy only; the stored row is untouched) with a bounded, attributed
`role: "user"` envelope:

```text
[external-participant-message id=pturn-8f21 from="Peer Agent" handle=@peer status=completed]
Looks fine to me.
[end-external-participant-message id=pturn-8f21]
```

Properties the projection guarantees:

- **User role, always.** Never system, developer, tool, or assistant. Untrusted
  peer text cannot gain system authority, cannot merge into a genuine Hermes
  assistant turn, and cannot silently impersonate the human — the frame labels
  provenance inside the turn it lands in.
- **Byte-deterministic.** Built only from persisted row fields, so rebuilding
  the same history produces identical bytes and the provider prompt-cache
  prefix stays stable.
- **Bounded.** Content is capped at 16,000 characters with a deterministic
  truncation marker, so one verbose peer cannot evict the context window.
- **Frame-safe.** Literal `[external-participant-message` /
  `[end-external-participant-message` sequences inside the content are escaped,
  and header values are rendered with quotes and brackets neutralized, so
  neither a reply's text nor a hostile display name can forge or terminate a
  frame.
- **Streaming rows are skipped.** A reply still in flight has no settled text
  to replay; it enters context on the next turn after it completes.
- `participant_directed` rows need no projection — they are already user rows.
  The alternation repair may merge them with an adjacent envelope or the next
  human message into a single user turn; the envelope markers keep each
  contribution attributable inside it. The current turn's enriched request
  bytes (memory prefetch, plugin context, attachment refs) are materialized
  into the request copy *before* that merge, so a merged turn still carries
  them and the durable `api_content` sidecar is unchanged.

Sessions with no participant rows take the exact same code path they did before
the seam existed.

## Events

All four events are session-scoped and carry their payload under
`params.payload`:

| event | payload |
| --- | --- |
| `participant.user_message` | `row_id`, `text`, `mentions`, `timestamp` |
| `participant.message.start` | `row_id`, `participant_turn_id`, `participant`, `timestamp` |
| `participant.message.delta` | `participant_turn_id`, `row_id`, `text` (this chunk) |
| `participant.message.complete` | `participant_turn_id`, `row_id`, `status`, `text` (full), `error?`, `timestamp` |

Deltas are UI-only: they never write to the database. The row is opened empty
by `begin_participant_message` and filled in once by
`complete_participant_message`. A surface that joins mid-stream therefore sees
the reply appear when it completes — the empty in-progress row carries no text
for the history projection to render.

## Ownership and errors

Every rejection is a `ParticipantSeamError` subtype, so one `except` clause
covers the whole seam. A bad call can never corrupt session history or take the
gateway loop down with it.

| exception | raised when |
| --- | --- |
| `ParticipantSeamError` | validation failure, duplicate turn id, storage failure |
| `UnknownSessionError` | the session id is unknown or no longer live |
| `OwnershipError` | the calling plugin does not own that participant or handle, or that turn |
| `UnknownTurnError` | no open turn matches `(session_id, participant_turn_id)` |

A participant id is owned by the plugin that first registered it in a session,
and so is its handle — a mention names a handle, so two plugins cannot both
answer to one. Another plugin cannot re-register either or publish against
them. Active turns are keyed `(session_id, participant_turn_id)`.

Completion is only reported once the row is actually updated. If the row is
gone by then — a rewind hard-deleted it, compaction rotated the session key —
`complete_participant_message` raises rather than returning quietly, because a
silent success would tell the caller a reply is in the transcript when nothing
is. Treat that failure as a failed participant turn.

Identity fields are structural input to the envelope header, not decoration, so
they are validated at registration: `handle` must match
`[a-z0-9][a-z0-9_-]{0,31}` (normalized to lowercase, leading `@` stripped),
`id` / `adapter_id` / `plugin_id` are capped at 128 characters, and
`display_name` is capped at 64 with control characters stripped.

## Concurrency and crash behaviour

Publisher functions are safe to call from any thread.

- `begin_participant_message` reserves its turn id before any side effect, so
  two concurrent begins cannot both publish a row. If persistence then fails,
  the reservation is released and the id is free for a retry.
- `complete_participant_message` snapshots the delta buffer and closes the turn
  in one critical section. A delta arriving at or after that point raises
  `UnknownTurnError` — it is never appended to the final text and never emitted
  after the row is final. If the finalizing write fails, the turn reopens so a
  retry (or continued streaming) still works.

Publishing from inside a running Hermes turn is supported: a Hermes tool call
that asks a peer agent publishes into the very turn it runs in, and the
gateway's end-of-turn reconciliation treats those rows as expected mid-turn
appends. Both the participant's reply and Hermes's own output survive, ordered
ask → peer reply → Hermes reply, matching the durable transcript.

The delta buffer lives in memory only. If the publishing process dies
mid-stream, the row stays `streaming` with empty content in the database until
some caller completes or replaces it — a known limitation, not a recoverable
state.
