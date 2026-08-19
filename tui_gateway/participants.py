"""In-process publisher seam for external agent participants.

A *participant* is any agent other than Hermes that speaks into a Hermes
session — another CLI agent driven by a plugin, a remote worker, a bridged
assistant. This module is the ONLY supported way to put such a voice into a
session: it persists the attributed row, keeps the live gateway history in
sync, and emits the session-scoped stream events the UI renders from.

The seam is deliberately generic. Nothing here knows which agent is speaking:
callers supply their own participant roster and own the turns they publish.

Contract summary (see ``website/docs/developer-guide/external-participants.md``):

* ``session_id`` is always the **gateway/UI session id** (the id in event
  frames and accepted by ``session.*`` RPCs), never the durable DB id. Core
  resolves it to the owning ``SessionDB`` row internally.
* A participant reply is one durable ``role="assistant"`` row carrying
  ``display_kind="participant_message"``; a human message addressed to a
  participant is one ``role="user"`` row carrying
  ``display_kind="participant_directed"``. No schema change, no new role.
* Every rejection is a :class:`ParticipantSeamError` subtype. A bad call can
  never corrupt session history or take the gateway loop down with it.

Crash semantics: the delta buffer lives in memory only, so a ``streaming``
row whose publishing process dies stays ``streaming`` with empty content in
the database until some caller completes or replaces it.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

#: ``display_kind`` of a participant's own reply row (role ``assistant``).
PARTICIPANT_MESSAGE_DISPLAY_KIND = "participant_message"
#: ``display_kind`` of a human message addressed to a participant (role ``user``).
PARTICIPANT_DIRECTED_DISPLAY_KIND = "participant_directed"

EVENT_USER_MESSAGE = "participant.user_message"
EVENT_MESSAGE_START = "participant.message.start"
EVENT_MESSAGE_DELTA = "participant.message.delta"
EVENT_MESSAGE_COMPLETE = "participant.message.complete"

STREAMING_STATUS = "streaming"
TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})

# Identity fields are interpolated into the model-context envelope HEADER that
# labels a peer's text, so they are untrusted input with a structural role, not
# decoration. Bound and charset-restrict them at the door; the envelope builder
# neutralizes header punctuation again at render time for rows written before
# these rules (or by anything else).
_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_MAX_ID_LEN = 128
_MAX_DISPLAY_NAME_LEN = 64
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

#: Gateway session-context variable naming the live UI session of the current
#: turn. The durable ``HERMES_SESSION_ID`` is deliberately NOT a fallback: it
#: names the DB row, not the window a tool call is running in.
UI_SESSION_ENV = "HERMES_UI_SESSION_ID"


class ParticipantSeamError(Exception):
    """Base class for every rejection raised by this module."""


class UnknownSessionError(ParticipantSeamError):
    """The gateway session id is unknown or no longer live."""


class OwnershipError(ParticipantSeamError):
    """The calling plugin does not own the participant or turn it addressed."""


class UnknownTurnError(ParticipantSeamError):
    """No active participant turn matches ``(session_id, participant_turn_id)``."""


# Active-turn lifecycle. A turn is RESERVED the instant its id is claimed (so a
# concurrent duplicate begin cannot publish a second row), OPEN once its row
# exists, and TERMINAL the instant completion snapshots the delta buffer — after
# which no delta can be appended or emitted.
_TURN_RESERVED = "reserved"
_TURN_OPEN = "open"
_TURN_TERMINAL = "terminal"

# Registry state. Keyed by gateway session id; entries for sessions the gateway
# has torn down are pruned on the next resolve, so a long-lived gateway does not
# accumulate rosters for dead sessions.
_registry_lock = threading.RLock()
_rosters: dict[str, dict[str, dict]] = {}
_active_turns: dict[tuple[str, str], dict] = {}


# ── validation helpers ──────────────────────────────────────────────────────


def _require_id(value: Any, field: str, *, max_len: int = _MAX_ID_LEN) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ParticipantSeamError(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > max_len:
        raise ParticipantSeamError(f"{field} must be at most {max_len} characters")
    return text


def _normalize_handle_text(value: Any) -> str:
    """One spelling of a handle: no ``@``, no surrounding space, lowercase.

    Shared by registration and mention parsing so a roster entry and a mention
    of it can never disagree on case or a leading ``@``.
    """
    return value.strip().lstrip("@").strip().lower() if isinstance(value, str) else ""


def _require_handle(value: Any) -> str:
    handle = _normalize_handle_text(value)
    if not _HANDLE_RE.match(handle):
        raise ParticipantSeamError(
            "handle must be 1-32 characters of [a-z0-9_-] starting with a letter or digit"
        )
    return handle


def _require_display_name(value: Any, fallback: str) -> str:
    text = value if isinstance(value, str) else ""
    text = _CONTROL_CHARS_RE.sub(" ", text).strip()
    if not text:
        return fallback
    if len(text) > _MAX_DISPLAY_NAME_LEN:
        raise ParticipantSeamError(
            f"display_name must be at most {_MAX_DISPLAY_NAME_LEN} characters"
        )
    return text


def _require_body(value: Any, field: str) -> str:
    """Validate free-form message text, returning it unaltered.

    Unlike an identifier, message text is persisted byte-for-byte — stripping
    it here would silently rewrite what the human typed.
    """
    if not isinstance(value, str) or not value.strip():
        raise ParticipantSeamError(f"{field} must be a non-empty string")
    return value


def _normalize_mentions(mentions: Any) -> list[str]:
    if mentions is None:
        return []
    if isinstance(mentions, str) or not isinstance(mentions, (list, tuple)):
        raise ParticipantSeamError("mentions must be a list of strings")
    out: list[str] = []
    for mention in mentions:
        if not isinstance(mention, str):
            raise ParticipantSeamError("mentions must be a list of strings")
        handle = _normalize_handle_text(mention)
        if handle and handle not in out:
            out.append(handle)
    return out


def _normalize_participant(entry: Any, plugin_id: str) -> dict:
    if not isinstance(entry, dict):
        raise ParticipantSeamError("each participant must be a dict")
    participant_id = _require_id(entry.get("id"), "participant id")
    handle = _require_handle(entry.get("handle"))
    adapter_id = entry.get("adapter_id")
    adapter_id = _require_id(adapter_id, "adapter_id") if adapter_id else ""
    return {
        "id": participant_id,
        "handle": handle,
        "display_name": _require_display_name(entry.get("display_name"), handle),
        "adapter_id": adapter_id,
        "status": str(entry.get("status") or "").strip() or "ready",
        "capabilities": (
            dict(entry["capabilities"]) if isinstance(entry.get("capabilities"), dict) else {}
        ),
        "plugin_id": plugin_id,
    }


def _attribution(record: dict) -> dict:
    """The ``participant`` block embedded in rows and events."""
    return {
        "id": record["id"],
        "handle": record["handle"],
        "display_name": record["display_name"],
        "plugin_id": record["plugin_id"],
        "adapter_id": record["adapter_id"],
    }


# ── gateway plumbing ────────────────────────────────────────────────────────


def _server():
    """Import the gateway lazily so this module stays cycle-free and cheap."""
    import tui_gateway.server as server

    return server


def _prune_dead_sessions(live_ids: set[str]) -> None:
    with _registry_lock:
        for dead in [sid for sid in _rosters if sid not in live_ids]:
            _rosters.pop(dead, None)
        for key in [key for key in _active_turns if key[0] not in live_ids]:
            _active_turns.pop(key, None)


def _resolve_session(session_id: Any, *, prune: bool = True) -> tuple[Any, str, dict, str]:
    """Map a gateway session id onto its live session and durable DB id.

    ``prune=False`` skips the dead-session sweep for the streaming delta path,
    which runs once per chunk: registry GC belongs on the once-per-turn calls
    (register / list / begin / complete), not in the middle of a stream.
    """
    sid = _require_id(session_id, "session_id")
    server = _server()
    with server._sessions_lock:
        session = server._sessions.get(sid)
        live_ids = set(server._sessions) if prune else None
    if live_ids is not None:
        _prune_dead_sessions(live_ids)
    if not isinstance(session, dict):
        raise UnknownSessionError(f"no live gateway session {sid!r}")
    session_key = str(session.get("session_key") or "").strip()
    if not session_key:
        raise UnknownSessionError(f"gateway session {sid!r} has no durable session id")
    return server, sid, session, session_key


def resolve_publish_session_id(explicit: str | None = None) -> str:
    """Return the live gateway session id publishing should be keyed to.

    An explicit id wins (validated against the live session table). Otherwise
    the calling context's UI session is used — a Hermes tool call runs inside
    the session that dispatched it, so a tool-dispatched participant message
    lands in the window the human is looking at. The durable
    ``HERMES_SESSION_ID`` is never consulted: it names the database row, and
    routing by it would publish into whichever session happens to share it.
    """
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit.strip():
            raise UnknownSessionError("explicit session id must be a non-empty string")
        _server_mod, sid, _session, _key = _resolve_session(explicit)
        return sid

    from gateway.session_context import get_session_env

    candidate = (get_session_env(UI_SESSION_ENV, "") or "").strip()
    if not candidate:
        raise UnknownSessionError(
            "no live UI session in context; pass an explicit gateway session id"
        )
    _server_mod, sid, _session, _key = _resolve_session(candidate)
    return sid


def _persist_row(
    server,
    session: dict,
    session_key: str,
    *,
    role: str,
    content: str,
    display_kind: str,
    display_metadata: dict,
    timestamp: float,
) -> int:
    try:
        server._ensure_session_db_row(session)
        with server._session_db(session) as db:
            if db is None:
                raise ParticipantSeamError("session store unavailable")
            return int(
                db.append_message(
                    session_id=session_key,
                    role=role,
                    content=content,
                    display_kind=display_kind,
                    display_metadata=display_metadata,
                    timestamp=timestamp,
                )
            )
    except ParticipantSeamError:
        raise
    except Exception as exc:
        # Storage boundary: re-raise (never swallow) as the seam's own type so
        # a plugin catching ParticipantSeamError catches every failure mode.
        raise ParticipantSeamError(f"participant row persist failed: {exc}") from exc


def _history_lock(session: dict):
    """The session's history lock, or a throwaway one for a record without it.

    Every session the gateway builds carries a ``history_lock``. A record that
    somehow lacks one has no lock for any other writer to hold either, so a
    fresh lock guards nothing but also excludes nobody — the alternative,
    refusing to publish, would lose the message over a condition that cannot
    make the append any safer.
    """
    lock = session.get("history_lock")
    return lock if lock is not None else threading.Lock()


def _append_history(session: dict, entry: dict) -> None:
    """Mirror a freshly persisted row into the live gateway history."""
    from agent.context_compressor import _DB_PERSISTED_MARKER

    # This row is already durable; the marker keeps the agent's own
    # append-only transcript flush from writing it a second time.
    entry[_DB_PERSISTED_MARKER] = True
    with _history_lock(session):
        history = session.setdefault("history", [])
        history.append(entry)
        session["history_version"] = int(session.get("history_version", 0)) + 1


def _update_history_row(session: dict, row_id: int, *, content: str, display_metadata: dict) -> None:
    with _history_lock(session):
        for entry in session.get("history") or []:
            if isinstance(entry, dict) and entry.get("_row_id") == row_id:
                entry["content"] = content
                entry["display_metadata"] = display_metadata
                break
        session["history_version"] = int(session.get("history_version", 0)) + 1


# ── public API ──────────────────────────────────────────────────────────────


def register_participants(session_id: str, plugin_id: str, participants: list[dict]) -> None:
    """Idempotently upsert *plugin_id*'s participant roster for one session.

    Re-registering the same participant id replaces its record. A participant
    id already owned by a different plugin is rejected without applying any
    part of the batch.
    """
    _server_mod, sid, _session, _key = _resolve_session(session_id)
    owner = _require_id(plugin_id, "plugin_id")
    if not isinstance(participants, (list, tuple)):
        raise ParticipantSeamError("participants must be a list of dicts")
    records = [_normalize_participant(entry, owner) for entry in participants]

    with _registry_lock:
        roster = _rosters.setdefault(sid, {})
        for record in records:
            existing = roster.get(record["id"])
            if existing is not None and existing["plugin_id"] != owner:
                raise OwnershipError(
                    f"participant {record['id']!r} is owned by plugin "
                    f"{existing['plugin_id']!r}"
                )
            # Handles are how humans address a participant, so a session cannot
            # hold two plugins' claims on the same one — the mention would be
            # ambiguous and route by registration order.
            for other in roster.values():
                if (
                    other["handle"] == record["handle"]
                    and other["id"] != record["id"]
                    and other["plugin_id"] != owner
                ):
                    raise OwnershipError(
                        f"handle @{record['handle']} is owned by plugin "
                        f"{other['plugin_id']!r} in this session"
                    )
        for record in records:
            roster[record["id"]] = record


def list_participants(session_id: str) -> list[dict]:
    """Return the registered roster for one session, in registration order."""
    _server_mod, sid, _session, _key = _resolve_session(session_id)
    with _registry_lock:
        roster = _rosters.get(sid) or {}
        return [
            dict(record, capabilities=dict(record["capabilities"]))
            for record in roster.values()
        ]


def append_participant_user_message(
    session_id: str, plugin_id: str, text: str, mentions: list[str]
) -> int:
    """Persist a human message addressed to participants; return its row id.

    The row stays a genuine ``role="user"`` message (the human really typed
    it), so it remains part of Hermes's model context. ``display_kind`` marks
    it as participant-directed so no surface mistakes it for a turn Hermes was
    asked to answer.
    """
    server, sid, session, session_key = _resolve_session(session_id)
    owner = _require_id(plugin_id, "plugin_id")
    body = _require_body(text, "text")
    handles = _normalize_mentions(mentions)
    metadata = {"mentions": handles, "plugin_id": owner}
    timestamp = time.time()

    row_id = _persist_row(
        server,
        session,
        session_key,
        role="user",
        content=body,
        display_kind=PARTICIPANT_DIRECTED_DISPLAY_KIND,
        display_metadata=metadata,
        timestamp=timestamp,
    )
    _append_history(
        session,
        {
            "role": "user",
            "content": body,
            "display_kind": PARTICIPANT_DIRECTED_DISPLAY_KIND,
            "display_metadata": metadata,
            "timestamp": timestamp,
            "_row_id": row_id,
        },
    )
    server._emit(
        EVENT_USER_MESSAGE,
        sid,
        {"row_id": row_id, "text": body, "mentions": handles, "timestamp": timestamp},
    )
    return row_id


def begin_participant_message(
    session_id: str, plugin_id: str, participant_id: str, participant_turn_id: str
) -> int:
    """Open a streaming participant reply; return the persisted row id."""
    server, sid, session, session_key = _resolve_session(session_id)
    owner = _require_id(plugin_id, "plugin_id")
    pid = _require_id(participant_id, "participant_id")
    turn_id = _require_id(participant_turn_id, "participant_turn_id")

    # Reserve the turn slot BEFORE any side effect: two concurrent begins with
    # the same turn id must not each persist a row, with one silently
    # overwriting the other's registry entry.
    with _registry_lock:
        record = (_rosters.get(sid) or {}).get(pid)
        if record is None or record["plugin_id"] != owner:
            raise OwnershipError(
                f"participant {pid!r} is not registered to plugin {owner!r} in this session"
            )
        if (sid, turn_id) in _active_turns:
            raise ParticipantSeamError(f"participant turn {turn_id!r} is already active")
        attribution = _attribution(record)
        turn = {
            "plugin_id": owner,
            "participant_id": pid,
            "attribution": attribution,
            "row_id": None,
            "buffer": [],
            "lock": threading.Lock(),
            "state": _TURN_RESERVED,
        }
        _active_turns[(sid, turn_id)] = turn

    metadata = {
        "participant": attribution,
        "participant_turn_id": turn_id,
        "status": STREAMING_STATUS,
    }
    timestamp = time.time()
    try:
        row_id = _persist_row(
            server,
            session,
            session_key,
            role="assistant",
            content="",
            display_kind=PARTICIPANT_MESSAGE_DISPLAY_KIND,
            display_metadata=metadata,
            timestamp=timestamp,
        )
    except ParticipantSeamError:
        # Nothing was published, so release the reservation and let a retry
        # use the same turn id.
        with _registry_lock:
            if _active_turns.get((sid, turn_id)) is turn:
                _active_turns.pop((sid, turn_id), None)
        raise

    _append_history(
        session,
        {
            "role": "assistant",
            "content": "",
            "display_kind": PARTICIPANT_MESSAGE_DISPLAY_KIND,
            "display_metadata": metadata,
            "timestamp": timestamp,
            "_row_id": row_id,
        },
    )
    with turn["lock"]:
        turn["row_id"] = row_id
        turn["state"] = _TURN_OPEN
    server._emit(
        EVENT_MESSAGE_START,
        sid,
        {
            "row_id": row_id,
            "participant_turn_id": turn_id,
            "participant": attribution,
            "timestamp": timestamp,
        },
    )
    return row_id


def _claim_turn(sid: str, turn_id: str, owner: str) -> dict:
    with _registry_lock:
        turn = _active_turns.get((sid, turn_id))
        if turn is None:
            raise UnknownTurnError(f"no active participant turn {turn_id!r} in this session")
        if turn["plugin_id"] != owner:
            raise OwnershipError(
                f"participant turn {turn_id!r} is owned by plugin {turn['plugin_id']!r}"
            )
        return turn


def _require_open(turn: dict, turn_id: str) -> None:
    """Caller must hold ``turn["lock"]``."""
    if turn["state"] != _TURN_OPEN:
        raise UnknownTurnError(f"participant turn {turn_id!r} is not open for writing")


def append_participant_delta(
    session_id: str, plugin_id: str, participant_turn_id: str, delta: str
) -> None:
    """Buffer one streamed chunk and mirror it to the UI. No database write."""
    server, sid, _session, _key = _resolve_session(session_id, prune=False)
    owner = _require_id(plugin_id, "plugin_id")
    turn_id = _require_id(participant_turn_id, "participant_turn_id")
    if not isinstance(delta, str):
        raise ParticipantSeamError("delta must be a string")
    turn = _claim_turn(sid, turn_id, owner)
    # The per-turn lock keeps buffer order and emitted order identical without
    # serializing every other session's publishes behind one global lock, and
    # it is the same critical section completion uses — so a delta is either
    # part of the final text or rejected, never emitted after finalization.
    with turn["lock"]:
        _require_open(turn, turn_id)
        if not delta:
            return
        turn["buffer"].append(delta)
        server._emit(
            EVENT_MESSAGE_DELTA,
            sid,
            {"participant_turn_id": turn_id, "row_id": turn["row_id"], "text": delta},
        )


def complete_participant_message(
    session_id: str,
    plugin_id: str,
    participant_turn_id: str,
    *,
    status: str = "completed",
    text: str | None = None,
    error: str | None = None,
) -> None:
    """Finalize a participant reply in the database, live history, and UI."""
    server, sid, session, session_key = _resolve_session(session_id)
    owner = _require_id(plugin_id, "plugin_id")
    turn_id = _require_id(participant_turn_id, "participant_turn_id")
    if status not in TERMINAL_STATUSES:
        raise ParticipantSeamError(
            f"status must be one of {sorted(TERMINAL_STATUSES)}, got {status!r}"
        )
    if text is not None and not isinstance(text, str):
        raise ParticipantSeamError("text must be a string or None")
    if error is not None and not isinstance(error, str):
        raise ParticipantSeamError("error must be a string or None")

    turn = _claim_turn(sid, turn_id, owner)
    # Snapshot the buffer and close the turn to writers in ONE critical
    # section: a delta racing this call is either already in `final_text` or
    # rejected outright, never appended (and emitted) after the final row.
    with turn["lock"]:
        _require_open(turn, turn_id)
        final_text = text if text is not None else "".join(turn["buffer"])
        turn["state"] = _TURN_TERMINAL

    metadata = {
        "participant": turn["attribution"],
        "participant_turn_id": turn_id,
        "status": status,
    }
    if error:
        metadata["error"] = error

    row_id = turn["row_id"]
    try:
        with server._session_db(session) as db:
            if db is None:
                raise ParticipantSeamError("session store unavailable")
            if not db.update_message_row(
                session_key,
                row_id,
                content=final_text,
                display_metadata=metadata,
            ):
                # The row is gone or no longer addressable under this session:
                # a rewind hard-deleted it, or compaction rotated the session
                # key out from under the turn. Reporting success here would
                # leave callers (and any chain routing) believing a reply is in
                # the transcript when nothing is.
                raise ParticipantSeamError(
                    f"participant row {row_id} is no longer part of session "
                    f"{session_key!r}; the reply was not stored"
                )
    except Exception as exc:
        # Nothing was finalized, so reopen the turn: the caller may retry, and
        # a participant that is still streaming may keep sending.
        with turn["lock"]:
            turn["state"] = _TURN_OPEN
        if isinstance(exc, ParticipantSeamError):
            raise
        raise ParticipantSeamError(f"participant row update failed: {exc}") from exc

    _update_history_row(session, row_id, content=final_text, display_metadata=metadata)
    with _registry_lock:
        _active_turns.pop((sid, turn_id), None)

    payload = {
        "participant_turn_id": turn_id,
        "row_id": row_id,
        "status": status,
        "text": final_text,
        "timestamp": time.time(),
    }
    if error:
        payload["error"] = error
    server._emit(EVENT_MESSAGE_COMPLETE, sid, payload)


__all__ = [
    "EVENT_MESSAGE_COMPLETE",
    "EVENT_MESSAGE_DELTA",
    "EVENT_MESSAGE_START",
    "EVENT_USER_MESSAGE",
    "PARTICIPANT_DIRECTED_DISPLAY_KIND",
    "PARTICIPANT_MESSAGE_DISPLAY_KIND",
    "STREAMING_STATUS",
    "TERMINAL_STATUSES",
    "OwnershipError",
    "ParticipantSeamError",
    "UnknownSessionError",
    "UnknownTurnError",
    "append_participant_delta",
    "append_participant_user_message",
    "begin_participant_message",
    "complete_participant_message",
    "list_participants",
    "register_participants",
    "resolve_publish_session_id",
]
