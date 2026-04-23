"""Local-client session ingress service.

Translates generic local-client actions (``send``, ``send_async``, ``state``,
``list``, ``reset``, ``interrupt``) into calls against the existing gateway
session machinery. This is the only local-client module that reaches into
``GatewayRunner`` internals; the transport layer stays product-neutral.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from gateway.config import Platform
from gateway.local_client_bridge import LocalClientIdentity, LocalClientRequest, send_timeout_seconds
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource

if TYPE_CHECKING:
    from gateway.run import GatewayRunner

logger = logging.getLogger(__name__)

INTERRUPT_REASON = "Local-client interrupt"
INVALIDATION_REASON = "local_client_interrupt"

_TRANSCRIPT_LIMIT = 50
_ALLOWED_ROLES = frozenset({"user", "assistant", "system", "tool"})
_SLUG_RE = re.compile(r"[^a-z0-9\-]+")


def _slug(value: str, max_len: int = 40) -> str:
    if not value:
        return ""
    collapsed = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return collapsed[:max_len].strip("-")


def _chat_id_prefix(label: str) -> str:
    return f"local-client:{_slug(label) or 'unknown'}:"


def build_local_source(identity: LocalClientIdentity) -> SessionSource:
    label_slug = _slug(identity.label) or "unknown"
    cs_slug = _slug(identity.client_session_id) or "default"
    return SessionSource(
        platform=Platform.LOCAL,
        chat_id=f"local-client:{label_slug}:{cs_slug}",
        chat_name=identity.label,
        chat_type="dm",
        user_id=f"local-client:{label_slug}",
        user_name=identity.label,
    )


class LocalClientSessionService:
    """Map local-client actions onto the gateway + session store."""

    def __init__(self, runner: "GatewayRunner") -> None:
        self._runner = runner

    async def dispatch(self, req: LocalClientRequest) -> Dict[str, Any]:
        source = build_local_source(req.client)
        session_key = self._runner._session_key_for_source(source)
        action = req.action
        if action == "send":
            return await self._send(req, source, session_key, async_mode=False)
        if action == "send_async":
            return await self._send(req, source, session_key, async_mode=True)
        if action == "state":
            return self._snapshot(session_key, source)
        if action == "list":
            return self._list(req.client)
        if action == "reset":
            return self._reset(session_key, source)
        if action == "interrupt":
            return await self._interrupt(session_key, source)
        # from_json already rejects invalid actions; defensive fallback.
        snap = self._snapshot(session_key, source)
        snap.update({"ok": False, "error": f"unsupported action: {action}"})
        return snap

    async def _send(
        self,
        req: LocalClientRequest,
        source: SessionSource,
        session_key: str,
        *,
        async_mode: bool,
    ) -> Dict[str, Any]:
        if not req.message:
            snap = self._snapshot(session_key, source)
            snap.update({"ok": False, "error": "message required for send/send_async"})
            return snap

        if session_key in getattr(self._runner, "_running_agents", {}):
            snap = self._snapshot(session_key, source)
            snap.update({"ok": True, "accepted": False, "busy": True, "running": True})
            return snap

        event = MessageEvent(
            text=req.message,
            message_type=MessageType.TEXT,
            source=source,
            internal=False,
        )

        if async_mode:
            task = asyncio.create_task(self._runner._handle_message(event))
            bg = getattr(self._runner, "_background_tasks", None)
            if isinstance(bg, set):
                bg.add(task)
                task.add_done_callback(bg.discard)
            snap = self._snapshot(session_key, source)
            snap.update({"ok": True, "accepted": True, "running": True})
            return snap

        task = asyncio.create_task(self._runner._handle_message(event))
        bg = getattr(self._runner, "_background_tasks", None)
        if isinstance(bg, set):
            bg.add(task)
            task.add_done_callback(bg.discard)

        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=send_timeout_seconds(),
            )
        except asyncio.TimeoutError:
            snap = self._snapshot(session_key, source)
            snap.update(
                {
                    "ok": True,
                    "accepted": True,
                    "running": True,
                    "detail": "Turn still running after sync timeout.",
                }
            )
            return snap

        snap = self._snapshot(session_key, source)
        snap.update({"ok": True, "accepted": True})
        return snap

    async def _interrupt(self, session_key: str, source: SessionSource) -> Dict[str, Any]:
        await self._runner._interrupt_and_clear_session(
            session_key,
            source,
            interrupt_reason=INTERRUPT_REASON,
            invalidation_reason=INVALIDATION_REASON,
            release_running_state=True,
        )
        snap = self._snapshot(session_key, source)
        snap.update({"interrupt_requested": True, "running": False, "busy": False})
        return snap

    def _reset(self, session_key: str, source: SessionSource) -> Dict[str, Any]:
        store = getattr(self._runner, "session_store", None)
        if store is not None:
            try:
                store.reset_session(session_key)
            except Exception as exc:
                logger.debug("reset_session(%s) failed: %s", session_key, exc)
        evict = getattr(self._runner, "_evict_cached_agent", None)
        if callable(evict):
            try:
                evict(session_key)
            except Exception as exc:
                logger.debug("_evict_cached_agent(%s) failed: %s", session_key, exc)
        invalidate = getattr(self._runner, "_invalidate_session_run_generation", None)
        if callable(invalidate):
            try:
                invalidate(session_key, reason="session_reset")
            except Exception as exc:
                logger.debug("_invalidate_session_run_generation(%s) failed: %s", session_key, exc)
        snap = self._snapshot(session_key, source, after_reset=True)
        snap.update({"ok": True, "detail": "Session reset."})
        return snap

    def _list(self, identity: LocalClientIdentity) -> Dict[str, Any]:
        prefix = _chat_id_prefix(identity.label)
        store = getattr(self._runner, "session_store", None)
        if store is None:
            return {"ok": True, "sessions": []}
        try:
            entries = store.list_sessions()
        except Exception as exc:
            logger.debug("list_sessions failed: %s", exc)
            return {"ok": True, "sessions": []}
        matching: List[SessionEntry] = [
            entry
            for entry in entries
            if entry.platform == Platform.LOCAL
            and entry.origin is not None
            and isinstance(entry.origin.chat_id, str)
            and entry.origin.chat_id.startswith(prefix)
        ]
        return {
            "ok": True,
            "sessions": [self._entry_brief(entry) for entry in matching],
        }

    def _snapshot(
        self,
        session_key: str,
        source: SessionSource,
        *,
        after_reset: bool = False,
    ) -> Dict[str, Any]:
        running_agents = getattr(self._runner, "_running_agents", {}) or {}
        running = session_key in running_agents
        messages: List[Dict[str, Any]] = []
        store = getattr(self._runner, "session_store", None)
        if store is not None and not after_reset:
            entries = getattr(store, "_entries", None)
            entry = entries.get(session_key) if isinstance(entries, dict) else None
            if entry is not None and getattr(entry, "session_id", None):
                try:
                    transcript = store.load_transcript(entry.session_id) or []
                except Exception as exc:
                    logger.debug("load_transcript(%s) failed: %s", entry.session_id, exc)
                    transcript = []
                messages = self._project_transcript(transcript[-_TRANSCRIPT_LIMIT:])
        return {
            "ok": True,
            "session_key": session_key,
            "running": running,
            "detail": "Turn in progress." if running else "Reply ready.",
            "interrupt_requested": False,
            "error": "",
            "messages": messages,
            "recent_events": [],
            "accepted": False,
            "busy": running,
        }

    @staticmethod
    def _project_transcript(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        projected: List[Dict[str, Any]] = []
        for msg in items:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role not in _ALLOWED_ROLES:
                continue
            projected.append(
                {
                    "role": role,
                    "content": msg.get("content"),
                    "tool_name": msg.get("tool_name"),
                }
            )
        return projected

    @staticmethod
    def _entry_brief(entry: SessionEntry) -> Dict[str, Any]:
        origin_chat_id: Optional[str] = None
        if entry.origin is not None:
            origin_chat_id = entry.origin.chat_id
        updated_at = entry.updated_at.isoformat() if entry.updated_at else None
        return {
            "session_key": entry.session_key,
            "session_id": entry.session_id,
            "chat_id": origin_chat_id,
            "updated_at": updated_at,
        }
