"""Approved-image media serving for the local-client bridge.

Only files under ``$HERMES_HOME/sessions/media`` are servable, and only when
both the extension and inferred MIME type are image types on the allowlist.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Optional

from aiohttp import web

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

ALLOWED_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
ALLOWED_MIME_PREFIXES = ("image/",)


def _approved_root() -> Path:
    return (get_hermes_home() / "sessions" / "media").resolve()


def _safe_resolve(raw: str, root: Path) -> Optional[Path]:
    try:
        target = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


class LocalClientMediaService:
    async def serve(self, request: web.Request) -> web.StreamResponse:
        raw = request.query.get("path", "").strip()
        if not raw:
            return web.Response(status=400, text="path required")

        root = _approved_root()
        if not root.exists():
            return web.Response(status=503, text="media root not initialized")

        target = _safe_resolve(raw, root)
        if target is None:
            return web.Response(status=403, text="path outside approved media root")

        if not target.is_file():
            return web.Response(status=404, text="not found")

        if target.suffix.lower() not in ALLOWED_EXTS:
            return web.Response(status=415, text="unsupported extension")

        mime, _ = mimetypes.guess_type(str(target))
        if not mime or not any(mime.startswith(p) for p in ALLOWED_MIME_PREFIXES):
            return web.Response(status=415, text="unsupported mime")

        return web.FileResponse(
            target,
            headers={
                "Content-Type": mime,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=300",
            },
        )
