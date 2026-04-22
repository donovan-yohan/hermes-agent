# Hermes Browser Sidecar Pairing

This branch exposes the Hermes-side bridge/backend required by the public `hermes-browser-sidecar` repo.

## What Hermes Provides

- A localhost browser bridge started by `hermes gateway`
- Health route: `GET /health`
- Page-context route: `POST /inject`
- Session route: `POST /session`
- Session actions currently used by the sidecar pairing:
  - `state`
  - `list`
  - `send`
  - `reset`
  - `interrupt`
- Media passthrough for bridge-rendered image attachments via `GET /media`
- `/browser connect|disconnect|status` in gateway/browser-sidecar sessions for live CDP wiring

The bridge is intentionally Hermes-private. The public sidecar repo should keep translating from its own local `/v1/...` protocol instead of treating these action names as a stable external API.

## Default Runtime

- Bridge host: `127.0.0.1`
- Bridge port: `8765`
- Token env var: `HERMES_BROWSER_BRIDGE_TOKEN`
- Token file fallback: `$HERMES_HOME/browser_bridge_token`
- Enable/disable env var: `HERMES_BROWSER_BRIDGE_ENABLED`
- Sync wait budget env var: `HERMES_BROWSER_BRIDGE_REQUEST_TIMEOUT_SECONDS`

## Pairing With `hermes-browser-sidecar`

1. Start Hermes gateway:

```bash
hermes gateway
```

2. Read the generated bridge token:

```bash
cat "${HERMES_HOME:-$HOME/.hermes}/browser_bridge_token"
```

3. Start the public sidecar service with bridge-backed transport:

```bash
HERMES_SIDECAR_TRANSPORT=hybrid \
HERMES_BROWSER_BRIDGE_URL=http://127.0.0.1:8765/inject \
HERMES_BROWSER_BRIDGE_TOKEN="$(cat "${HERMES_HOME:-$HOME/.hermes}/browser_bridge_token")" \
hermes-browser-sidecar serve
```

4. Load the extension from the public sidecar repo and point it at `http://127.0.0.1:8787`.

## Optional Live Browser Control

If you want Hermes browser tools to act on your real Chrome/Chromium session instead of the default headless browser, connect a CDP endpoint inside the gateway session:

```text
/browser connect chrome
/browser status
/browser disconnect
```

You can also pass an explicit endpoint such as `ws://localhost:9222`.

## Current Scope

This pairing is enough for:

- health/capabilities probing through the public sidecar service
- session state and history
- sending messages with optional page context
- reset/new chat
- interrupt
- bridge-backed media URLs for image attachments already present in Hermes history

It does not make the Hermes bridge a public compatibility promise. The stable client-facing surface remains the public sidecar repo’s local service.
