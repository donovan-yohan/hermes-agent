# Browser sidecar architecture: smallest Hermes core seam

Date: 2026-04-22  
Repo: `hermes-agent`  
Related reviews: Hermes PR #4 (`https://github.com/donovan-yohan/hermes-agent/pull/4`), paired sidecar PR #1 (`https://github.com/donovan-yohan/hermes-browser-sidecar/pull/1`)

## Why this spec exists

This spec captures the architecture conclusion from the earlier Hermes/sidecar review and the follow-up plugin review:

- **Hermes PR #4 is too heavy.**
- **The paired sidecar PR #1 is too thin.**
- The right split is **not** "put the browser product inside Hermes" and **not** "pretend a normal Hermes plugin owns the browser gateway".
- Hermes should keep a **small, explicit local-client ingress seam** for browser/sidecar clients.
- The paired `hermes-browser-sidecar` repo should consume that seam and own the browser-specific product surface.

This is a repo-specific implementation spec for future refactors in `hermes-agent`. It is intentionally conservative: keep the smallest core needed for correctness, security, and session ownership; move browser product logic out.

## Decision summary

### Final architecture

Hermes should expose a **core local client seam** that lets a localhost sidecar submit user turns into Hermes-managed sessions and fetch Hermes-managed session state.

That seam belongs in Hermes core because it owns:

1. **Gateway/session ingress**
2. **Session identity and transcript persistence**
3. **Turn execution against the real Hermes gateway/agent stack**
4. **Gateway-safe attachment/media serving for local clients**
5. **Auth and localhost trust boundary for local sidecars**

Everything else that is browser-product-specific should live outside core, primarily in the paired sidecar repo.

### Explicit conclusion on plugins

Hermes general plugins are still useful and should remain the extension point for:

- tools
- hooks
- slash commands
- skills
- optional CLI helpers

But they **cannot honestly own**:

- gateway ingress
- session semantics
- session creation/reset/interrupt rules
- transcript serialization contracts for a local browser client
- media serving for local browser-delivered images
- gateway-safe browser client injection

The hard stop is **gateway/session ownership**. Once a feature needs to create or route first-class gateway sessions, it is no longer a normal multi-select plugin concern.

## Current evidence in this repo

### Existing plugin API is not the right ownership boundary

`hermes_cli/plugins.py` exposes a broad general plugin surface:

- `PluginContext.register_tool()`
- `PluginContext.register_hook()`
- `PluginContext.register_command()`
- `PluginContext.register_cli_command()`
- `PluginContext.inject_message()`

Relevant constraints already visible in code/docs:

- `ctx.inject_message()` is CLI-oriented and explicitly warns when no CLI reference exists; it is not a gateway ingress API (`hermes_cli/plugins.py`).
- General plugins do not receive or own `SessionSource`, `SessionStore`, or gateway dispatch internals (`gateway/run.py`).
- A browser sidecar needs stable ownership of session routing, not best-effort message injection.

### Important doc/code mismatch to fix

Hermes docs currently state that general plugins can add CLI commands via `ctx.register_cli_command()`:

- `website/docs/guides/build-a-hermes-plugin.md`
- `website/docs/user-guide/features/plugins.md`

But `hermes_cli/main.py` currently wires dynamic CLI command discovery only from memory-provider plugin discovery:

- `hermes_cli/main.py` imports `plugins.memory.discover_plugin_cli_commands()` around lines 7499-7508.
- There is no equivalent hookup for general plugin manager `_cli_commands`.

That mismatch matters for this architecture discussion because it is another example of why we should not oversell general plugins as an answer to browser-side ingress.

### Current browser bridge code is too product-heavy for Hermes core

Today Hermes already contains browser-sidecar behavior in core files:

- `gateway/browser_bridge.py`
- `gateway/run.py`

The current implementation includes all of the following in Hermes:

- localhost HTTP serving (`/inject`, `/session`, `/health`, `/media`)
- token auth and token-file bootstrapping
- payload normalization for browser-specific fields
- browser-specific message rendering (`build_browser_context_message()`)
- browser-side session list/state/reset/interrupt/send/send_async behavior
- browser-specific transcript serialization and progress state
- browser-upload image fetching and local serving

The seam itself is valid. The amount of browser product logic inside Hermes is not.

### Related browser state already leaks across process boundaries

`tools/browser_tool.py` already persists shared browser runtime state (`read_shared_cdp_state()`, `persist_shared_cdp_state()`, `clear_shared_cdp_state()`). That is further evidence that a real client/server boundary exists and should be named explicitly instead of being disguised as a plugin.

## Why this belongs in Hermes core instead of a general plugin

### 1. Hermes owns gateway-safe ingress

A sidecar request is not just "a plugin received some JSON". It is a request to:

- authenticate a local client
- map it onto a stable session identity
- construct a `SessionSource`
- create or reuse a persisted transcript
- execute a Hermes turn through gateway dispatch
- return a session snapshot

Those are core runtime operations, not optional add-ons.

### 2. Hermes owns session semantics

The sidecar needs stable answers to questions like:

- what makes two browser turns the same session?
- what does reset mean?
- how does interrupt work if a Hermes turn is already running?
- which transcript messages are returned to the client?
- how are slash-command turns represented?

Those semantics currently live in `gateway/run.py`. A general plugin cannot safely own them because it does not own the gateway execution model.

### 3. Hermes owns trusted media handoff

A browser sidecar may send image references and then ask Hermes to return them in a session transcript. Safe re-serving of locally downloaded images is a core trust-boundary concern. The `/media` route in `gateway/browser_bridge.py` exists because Hermes must control what local files are re-exposed.

### 4. Hermes plugins are multi-select; ingress must be singular and authoritative

General plugins are designed for additive behavior. Local client ingress is not additive. There must be one authoritative contract for:

- auth
- session routing
- transcript shape
- running-turn state
- interrupt semantics

If multiple plugins could each define their own ingress/session model, Hermes would be lying about where authority lives.

### 5. Existing plugin docs already overstate capability

The current docs present plugins as broad integration points, but the code already shows meaningful limits. The browser-sidecar work should not deepen that mismatch by introducing fake modularity.

## The minimum Hermes core seam

Hermes should keep exactly one narrowly-scoped core facility for local browser/sidecar clients.

### Name

Working name for planning purposes:

- **local client bridge**
- or **local session ingress**

This spec uses **local client bridge seam**.

### Core contract

Hermes core should expose a localhost-only contract that supports exactly these operations:

1. **health**
   - confirm Hermes local bridge availability
2. **ingest one turn into a Hermes-managed session**
   - submit text
   - optionally include structured reference context
   - optionally include media attachments already accepted by Hermes
3. **inspect session state**
   - fetch session snapshot, transcript excerpt, and current running/progress state
4. **list sessions for this local client family**
5. **reset a session**
6. **interrupt the active turn for a session**
7. **serve approved local image media back to the local client**

That is the seam. No browser-product heuristics beyond what is required to support those operations.

### Required request model

Hermes core should standardize a browser-agnostic local-client request model. At minimum:

```json
{
  "client": {
    "kind": "browser-sidecar",
    "label": "Chrome Extension",
    "client_session_id": "optional-stable-client-session-id"
  },
  "action": "send|send_async|state|list|reset|interrupt",
  "message": "optional user text",
  "context": {
    "type": "reference_material",
    "title": "optional",
    "url": "optional",
    "selection": "optional",
    "page_text": "optional",
    "metadata": {},
    "attachments": []
  }
}
```

Notes:

- `client.kind` should stay generic enough for future localhost clients, but the first consumer is the paired browser sidecar repo.
- Hermes core should not bake in Discord-thread-specific or YouTube-transcript-specific fields as first-class core semantics.
- Browser-specific extraction may still exist, but it belongs in the sidecar repo and should be flattened into the generic `context` block before submission.

### Required response model

At minimum, Hermes should return:

```json
{
  "ok": true,
  "session_key": "hermes-session-key",
  "running": false,
  "detail": "Reply ready.",
  "interrupt_requested": false,
  "error": "",
  "messages": [],
  "recent_events": [],
  "accepted": true,
  "busy": false
}
```

The response should describe Hermes session state, not browser product state.

### Smallest implementation seam in code

The smallest useful seam inside this repo is:

1. a **transport/auth wrapper** for localhost requests
2. a **session ingress service** that translates local-client actions into gateway/session operations
3. a **minimal transcript serializer** for local-client consumption
4. a **minimal media-serving helper** for Hermes-owned approved image paths

A practical target module split is:

- `gateway/local_client_bridge.py` — transport, auth, route dispatch, generic request/response DTOs
- `gateway/local_client_sessions.py` — session ingress/state/reset/interrupt/send logic
- `gateway/local_client_media.py` — approved local image serving helpers
- `gateway/run.py` — only thin composition/wiring into `GatewayRunner`

This spec does **not** require those exact filenames, but the seam should end up that small.

## Responsibilities Hermes should keep vs move out

### Hermes core must keep

#### Session and ingress authority

- localhost auth/token validation
- mapping `(client label, client_session_id)` to Hermes `SessionSource`/session key
- session creation/reuse/reset
- running-turn tracking
- interrupt semantics
- gateway dispatch into `_handle_message()` or its successor
- transcript persistence and session snapshot generation

#### Generic local-client contract

- action routing: `send`, `send_async`, `state`, `list`, `reset`, `interrupt`
- browser-agnostic request validation
- stable response schema
- stable message/history serialization for local clients

#### Safe image/media serving

- approved local image serving for items already accepted into Hermes session history
- localhost-only serving and auth checks
- path validation and MIME restrictions

#### Minimal documentation in Hermes

- local-client/browser-bridge contract
- plugin limitations for ingress/session ownership
- doc-drift fix around `ctx.register_cli_command()`

### Hermes core should move out

Move to the paired `hermes-browser-sidecar` repo:

- browser extension UX
- sidecar daemon UX and packaging
- page scraping/extraction heuristics
- browser-specific payload field collection
- YouTube transcript fetching policy
- Discord thread scraping/flattening
- browser-specific prompt wording tweaks that are not required for contract correctness
- session picker UI
- sidecar retry/backoff UX
- browser brand/platform support matrix
- any browser-product feature flags or onboarding flows

### Borderline logic: where it should land

#### `build_browser_context_message()`

This should mostly move out of Hermes.

Hermes may keep a tiny helper that converts generic `context.reference_material` into a user-message block, but the current browser-specific shaping in `gateway/browser_bridge.py` is too opinionated for core.

#### Browser payload normalization

`normalize_payload()` in its current form is sidecar product logic, not core contract logic. Hermes should validate a generic local-client schema; sidecar-specific normalization belongs in `hermes-browser-sidecar`.

#### Session transcript shaping

Hermes should keep transcript serialization, but only in a generic form. Browser-specific labels like `"[Injected browser context from the local Chrome extension]"` should not be core protocol markers.

## Proposed target contract for the paired sidecar repo

The paired repo `hermes-browser-sidecar` should become the primary browser-facing implementation of this seam.

### Sidecar consumption model

The sidecar repo should:

1. collect page/browser context
2. normalize browser-specific fields into the Hermes local-client contract
3. authenticate to Hermes over localhost
4. submit/send/reset/interrupt/state/list calls
5. render Hermes responses in browser UI
6. optionally consume Hermes media URLs for session images

### Sidecar should not reimplement

The sidecar repo should not own or fork:

- Hermes session keys
- transcript persistence rules
- turn-running state truth
- interrupt semantics
- media-serving trust checks

Those must remain authoritative in Hermes core.

## Phased refactor plan

The goal is to shrink Hermes PR #4 into the seam above while giving `hermes-browser-sidecar` a real contract to build on.

### Phase 0: land this spec only

Files:

- `docs/superpowers/specs/2026-04-22-browser-sidecar-core-seam.md`

Verification:

```bash
cd /private/tmp/codex-hermes-sidecar-arch-hermes
test -f docs/superpowers/specs/2026-04-22-browser-sidecar-core-seam.md
```

### Phase 1: define and isolate the core seam in Hermes

Goal: separate generic local-client ingress from browser product logic without changing end-user behavior yet.

Suggested file work:

- refactor `gateway/browser_bridge.py`
- refactor `gateway/run.py`
- add `gateway/local_client_bridge.py`
- add `gateway/local_client_sessions.py`
- optionally add `gateway/local_client_media.py`

Tasks:

1. Extract transport/auth/route handling from `gateway/browser_bridge.py` into a generic local-client bridge module.
2. Extract send/state/list/reset/interrupt/session snapshot logic from `gateway/run.py` into a small service object.
3. Keep route compatibility for the current sidecar during transition.
4. Rename browser-specific internals where needed so future local clients are possible without duplicating transport.

Verification:

```bash
cd /private/tmp/codex-hermes-sidecar-arch-hermes
pytest -q tests/gateway/test_browser_bridge_context.py
pytest -q tests/gateway -k browser_bridge
```

If tests do not exist for generic session actions yet, add them in a later implementation PR, not in this spec-only change.

### Phase 2: shrink Hermes request semantics to generic local-client primitives

Goal: stop encoding browser product fields as core protocol.

Suggested file work:

- refactor `gateway/browser_bridge.py`
- add or update tests under `tests/gateway/`
- update any sidecar-facing examples/docs

Tasks:

1. Define a generic local-client request schema.
2. Accept legacy browser payloads temporarily through a compatibility adapter.
3. Move `normalize_payload()`-style field flattening out of core or behind a compatibility layer.
4. Reduce `build_browser_context_message()` to either:
   - a generic reference-material renderer, or
   - a compatibility shim scheduled for removal.

Verification:

```bash
cd /private/tmp/codex-hermes-sidecar-arch-hermes
pytest -q tests/gateway -k "browser_bridge or local_client"
```

### Phase 3: move browser product logic to the paired sidecar repo

Primary repo for work:

- `hermes-browser-sidecar`

Tasks in sidecar repo:

1. Own browser-specific payload capture and normalization.
2. Own browser UI/session picker/history rendering UX.
3. Own product-specific collection paths for page text, selection, transcripts, and thread content.
4. Target the Hermes local-client seam as the only ingress contract.

Hermes-side verification:

- confirm Hermes still supports a local authenticated client over localhost
- confirm no browser-product behavior is required in Hermes to complete core flows

### Phase 4: documentation cleanup and truth-telling

Update Hermes docs:

- `website/docs/guides/build-a-hermes-plugin.md`
- `website/docs/user-guide/features/plugins.md`
- `website/docs/developer-guide/architecture.md`
- `website/docs/user-guide/features/overview.md`
- optionally add a new developer doc for the local-client/browser-bridge contract

Required doc tasks:

1. **Plugin limitations**
   - explicitly state that general plugins do not own gateway ingress, session semantics, or trusted local-client media serving.
2. **Local-client/browser-bridge contract**
   - document the supported localhost routes/actions, auth model, request/response shape, and session semantics.
3. **Doc drift fix: CLI commands**
   - either wire general plugin CLI registration in `hermes_cli/main.py`, or update docs to say only memory-provider plugin CLI discovery is currently wired.
4. **Architecture docs**
   - show the local-client seam as part of gateway/core architecture, not as a normal plugin capability.
5. **Features overview**
   - if browser-sidecar support is user-visible, describe it as a local-client integration built on Hermes core ingress, not as a plugin trick.

Verification:

```bash
cd /private/tmp/codex-hermes-sidecar-arch-hermes/website
npm run build
```

### Phase 5: remove fake-modular leftovers

Goal: after the paired sidecar consumes the seam, remove browser-product residue from Hermes.

Tasks:

1. Delete compatibility code that only exists to preserve browser-specific payload shapes.
2. Remove browser-product text markers from transcript serialization if they are no longer contractually necessary.
3. Ensure remaining Hermes code reads as a generic local-client ingress service.

Verification:

```bash
cd /private/tmp/codex-hermes-sidecar-arch-hermes
pytest -q tests/gateway
pytest -q tests/hermes_cli
```

## Documentation notes for implementers

### Official/local docs that should be considered source material

- `website/docs/guides/build-a-hermes-plugin.md`
- `website/docs/developer-guide/memory-provider-plugin.md`
- `website/docs/developer-guide/context-engine-plugin.md`
- `website/docs/developer-guide/architecture.md`
- `website/docs/user-guide/features/overview.md`
- `website/docs/user-guide/features/plugins.md`

### Code that should anchor the refactor

- `hermes_cli/plugins.py`
- `hermes_cli/main.py`
- `gateway/run.py`
- `gateway/browser_bridge.py`
- `tools/browser_tool.py`

## Non-goals

- turning the browser sidecar into a general plugin
- moving gateway/session authority out of Hermes
- designing a full remote/network multi-tenant API surface
- replacing Hermes browser automation tools
- solving all browser UX in Hermes core
- introducing a broad new plugin type unless the minimal seam proves insufficient

## Fake-modularity warnings

Do **not** do any of the following:

1. **Do not wrap gateway-owned code in a plugin-shaped adapter and call it modular.**
   - If Hermes still owns session ingress and the plugin just forwards into private gateway methods, nothing meaningful was modularized.

2. **Do not keep browser-specific protocol fields in core and call them generic later.**
   - If the contract still revolves around Discord thread text, YouTube transcript flags, and extension-specific markers, the product logic did not move.

3. **Do not document capabilities the CLI/runtime does not actually wire.**
   - The existing `ctx.register_cli_command()` mismatch is the exact kind of drift to avoid.

4. **Do not split authority over session state.**
   - The sidecar may cache UI state, but Hermes must remain source of truth for conversation/session state.

5. **Do not make the seam larger than necessary.**
   - The goal is a tiny ingress seam, not a second public platform framework inside Hermes.

## Acceptance criteria

This architecture is successful when all of the following are true:

- Hermes exposes a small, documented localhost local-client seam.
- Hermes remains authoritative for session ingress, execution, interrupt, and transcript state.
- The paired `hermes-browser-sidecar` repo can build a real browser product using that seam.
- Browser-specific extraction/product logic no longer dominates Hermes core implementation.
- Hermes plugin docs clearly state the boundary: plugins are for tools/hooks/commands/skills, not gateway/session ownership.
- The CLI command documentation is either made true in code or corrected in docs.

## Bottom line

The right answer is **not** "browser sidecar as a normal plugin" and **not** "browser product embedded deeply into Hermes".

The right answer is:

- keep a **small Hermes core seam for local-client/session ingress**, and
- let the paired `hermes-browser-sidecar` repo own the browser product built on top of it.
