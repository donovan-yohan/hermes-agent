# Browser Sidecar Core Seam — Phase 1 Implementation

## Goal
Implement the smallest viable Hermes core seam for local browser/sidecar clients. This is Phase 1 of the architecture spec in `docs/superpowers/specs/2026-04-22-browser-sidecar-core-seam.md`.

## What to build

Create a new localhost-only ingress facility in `gateway/` that lets a local sidecar:
1. authenticate over localhost
2. submit a user turn into a Hermes-managed session
3. inspect session state/transcript
4. list sessions
5. reset a session
6. interrupt the active turn
7. fetch approved local image media

## Files to create/modify

### New files (keep them small and generic)
- `gateway/local_client_bridge.py` — transport, auth, route dispatch, generic request/response DTOs. NO browser-specific logic. Target ~150-200 lines.
- `gateway/local_client_sessions.py` — session ingress service that maps local-client actions into gateway/session operations. Target ~200-250 lines.
- `gateway/local_client_media.py` — approved local image serving with path validation and MIME restrictions. Target ~50-80 lines.

### Modified files (thin wiring only)
- `gateway/run.py` — add thin composition/wiring to register the local client bridge. Do NOT embed browser product logic here.

## Architecture constraints

- The request model must be browser-agnostic. Use `client.kind`, `client.label`, `client_session_id` for client identity.
- Actions: `send`, `send_async`, `state`, `list`, `reset`, `interrupt`.
- The response model must describe Hermes session state, not browser product state.
- Do NOT copy the old `gateway/browser_bridge.py` approach from PR #4 — that was too heavy and embedded browser-specific heuristics.
- Do NOT add Discord-thread-specific, YouTube-transcript-specific, or Chrome-extension-specific fields as core semantics.
- Session ownership stays in Hermes. The sidecar is just a local client.

## Reference material

Read these files before starting:
- `docs/superpowers/specs/2026-04-22-browser-sidecar-core-seam.md` (the full architecture spec)
- `gateway/run.py` — inspect how `_handle_message()`, session keys, and `SessionSource` work
- `gateway/session.py` — inspect session persistence
- `tools/browser_tool.py` — note `read_shared_cdp_state()` as evidence of client/server boundary

There is a prior PR branch at `origin/feat/browser-sidecar-wireup-20260422` with a `gateway/browser_bridge.py` (656 lines). You may inspect it for salvageable auth/transport patterns, but do NOT follow its architecture — it embedded too much product logic into core.

## Verification

After implementation:
1. `pytest -q tests/gateway/ -k local_client` should pass (add tests if none exist).
2. `pytest -q tests/gateway/ -k browser_bridge` should still pass (backward compat).
3. `scripts/run_tests.sh tests/gateway/` should pass.
4. The new modules should be importable without errors.

## Acceptance criteria
- Hermes exposes a small, documented localhost local-client seam
- Hermes remains authoritative for session ingress, execution, interrupt, and transcript state
- No browser-specific extraction/product logic lives in the new core modules
- The seam is generic enough for future localhost clients, not just browser sidecars
