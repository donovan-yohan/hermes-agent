"""Generic localhost ingress seam for local clients (e.g. browser sidecars).

This module owns transport, auth, route dispatch, and the generic request/
response DTOs. It intentionally contains no browser-, Discord-, YouTube-, or
extension-specific logic. Product-specific normalization belongs in the
consuming client (e.g. the paired ``hermes-browser-sidecar`` repo).

Architecture spec: ``docs/superpowers/specs/2026-04-22-browser-sidecar-core-seam.md``.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from aiohttp import web

from hermes_constants import get_hermes_home

if TYPE_CHECKING:
    from gateway.run import GatewayRunner

logger = logging.getLogger(__name__)

ENV_TOKEN = "HERMES_LOCAL_CLIENT_TOKEN"
ENV_PORT = "HERMES_LOCAL_CLIENT_PORT"
ENV_ENABLED = "HERMES_LOCAL_CLIENT_ENABLED"
ENV_SEND_TIMEOUT = "HERMES_LOCAL_CLIENT_SEND_TIMEOUT"

DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
DEFAULT_SEND_TIMEOUT_SECONDS = 120.0
TOKEN_FILE_NAME = "local_client_token"
AUTH_HEADER_ALT = "X-Hermes-Local-Client-Token"
SERVICE_LABEL = "local-client-bridge"

VALID_ACTIONS = frozenset({"send", "send_async", "state", "list", "reset", "interrupt"})

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class LocalClientValidationError(ValueError):
    """Raised when an inbound request fails schema validation."""


@dataclass(frozen=True)
class LocalClientIdentity:
    """Generic local-client identity. No browser/product fields."""

    kind: str
    label: str
    client_session_id: str


@dataclass(frozen=True)
class LocalClientRequest:
    client: LocalClientIdentity
    action: str
    message: Optional[str] = None
    context: Optional[Dict[str, Any]] = None

    @classmethod
    def from_json(cls, body: Any) -> "LocalClientRequest":
        if not isinstance(body, dict):
            raise LocalClientValidationError("request body must be a JSON object")

        client_raw = body.get("client")
        if not isinstance(client_raw, dict):
            raise LocalClientValidationError("client object required")

        kind = _require_nonempty_str(client_raw, "client.kind")
        label = _require_nonempty_str(client_raw, "client.label")
        client_session_id = _require_nonempty_str(client_raw, "client.client_session_id")

        action = body.get("action")
        if not isinstance(action, str) or action not in VALID_ACTIONS:
            raise LocalClientValidationError(
                f"action must be one of {sorted(VALID_ACTIONS)}"
            )

        message = body.get("message")
        if message is not None and not isinstance(message, str):
            raise LocalClientValidationError("message must be a string when provided")

        context = body.get("context")
        if context is not None and not isinstance(context, dict):
            raise LocalClientValidationError("context must be an object when provided")

        return cls(
            client=LocalClientIdentity(
                kind=kind, label=label, client_session_id=client_session_id
            ),
            action=action,
            message=message,
            context=context,
        )


def _require_nonempty_str(obj: Dict[str, Any], dotted: str) -> str:
    key = dotted.split(".", 1)[1] if "." in dotted else dotted
    val = obj.get(key)
    if not isinstance(val, str) or not val.strip():
        raise LocalClientValidationError(f"{dotted} required (non-empty string)")
    return val


def _token_file_path() -> Path:
    return get_hermes_home() / TOKEN_FILE_NAME


def _is_truthy(val: Optional[str]) -> bool:
    return bool(val) and val.strip().lower() in _TRUTHY


def resolve_token() -> Optional[str]:
    """Resolve the bridge auth token.

    Order: env var → ``$HERMES_HOME/local_client_token`` file → autogen.
    Writes a newly-generated token to disk with 0600 perms so subsequent
    gateway runs pick it up without reconfiguring the client. Returns
    ``None`` only if the caller explicitly opts out upstream.
    """
    env_val = os.environ.get(ENV_TOKEN, "").strip()
    if env_val:
        return env_val

    path = _token_file_path()
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError as exc:
        logger.warning("Failed to read %s: %s", path, exc)

    generated = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generated + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        logger.info("Generated new local-client bridge token at %s", path)
    except OSError as exc:
        logger.warning("Failed to persist generated token at %s: %s", path, exc)
    return generated


def _extract_request_token(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer ") :].strip()
    alt = request.headers.get(AUTH_HEADER_ALT, "")
    if alt:
        return alt.strip()
    return ""


def _timing_safe_auth(request: web.Request, expected: Optional[str]) -> bool:
    if not expected:
        return False
    supplied = _extract_request_token(request)
    if not supplied:
        return False
    return secrets.compare_digest(supplied, expected)


def _build_app(bridge: "LocalClientBridge") -> web.Application:
    app = web.Application()
    app.router.add_get("/health", bridge._handle_health)
    app.router.add_post("/v1/local-client/request", bridge._handle_request)
    app.router.add_get("/v1/local-client/media", bridge._handle_media)
    return app


class LocalClientBridge:
    """Thin aiohttp facade over the local-client ingress service."""

    def __init__(self, runner: "GatewayRunner") -> None:
        from gateway.local_client_media import LocalClientMediaService
        from gateway.local_client_sessions import LocalClientSessionService

        self._runner = runner
        self._token: Optional[str] = None
        self._app: Optional[web.Application] = None
        self._app_runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._sessions = LocalClientSessionService(runner)
        self._media = LocalClientMediaService()

    @property
    def enabled(self) -> bool:
        if _is_truthy(os.environ.get(ENV_ENABLED)):
            return True
        if os.environ.get(ENV_TOKEN, "").strip():
            return True
        try:
            return _token_file_path().is_file()
        except OSError:
            return False

    @property
    def token(self) -> Optional[str]:
        return self._token

    async def start(self) -> None:
        if not self.enabled:
            logger.info("LocalClientBridge: disabled (no token, no opt-in)")
            return
        self._token = resolve_token()
        if not self._token:
            logger.warning("LocalClientBridge: token resolution returned empty; skipping bind")
            return
        self._app = _build_app(self)
        self._app_runner = web.AppRunner(self._app)
        await self._app_runner.setup()
        port = _port_from_env()
        self._site = web.TCPSite(self._app_runner, host=DEFAULT_HOST, port=port)
        await self._site.start()
        logger.info("LocalClientBridge listening on %s:%d", DEFAULT_HOST, port)

    async def stop(self) -> None:
        if self._site is not None:
            try:
                await self._site.stop()
            finally:
                self._site = None
        if self._app_runner is not None:
            try:
                await self._app_runner.cleanup()
            finally:
                self._app_runner = None

    async def _handle_health(self, _request: web.Request) -> web.StreamResponse:
        return web.json_response({"ok": True, "service": SERVICE_LABEL})

    async def _handle_request(self, request: web.Request) -> web.StreamResponse:
        if not _timing_safe_auth(request, self._token):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            return web.json_response(
                {"ok": False, "error": f"invalid JSON: {exc}"}, status=400
            )
        try:
            lcr = LocalClientRequest.from_json(body)
        except LocalClientValidationError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        snapshot = await self._sessions.dispatch(lcr)
        return web.json_response(snapshot)

    async def _handle_media(self, request: web.Request) -> web.StreamResponse:
        if not _timing_safe_auth(request, self._token):
            query_tok = request.query.get("token", "")
            if not (
                self._token
                and query_tok
                and secrets.compare_digest(query_tok, self._token)
            ):
                return web.Response(status=401, text="unauthorized")
        return await self._media.serve(request)


def _port_from_env() -> int:
    raw = os.environ.get(ENV_PORT, "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; falling back to %d", ENV_PORT, raw, DEFAULT_PORT)
        return DEFAULT_PORT
    if not (0 < port < 65536):
        logger.warning("Out-of-range %s=%d; falling back to %d", ENV_PORT, port, DEFAULT_PORT)
        return DEFAULT_PORT
    return port


def send_timeout_seconds() -> float:
    raw = os.environ.get(ENV_SEND_TIMEOUT, "").strip()
    if not raw:
        return DEFAULT_SEND_TIMEOUT_SECONDS
    try:
        val = float(raw)
    except ValueError:
        return DEFAULT_SEND_TIMEOUT_SECONDS
    if val <= 0:
        return DEFAULT_SEND_TIMEOUT_SECONDS
    return val
