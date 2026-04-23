"""Media-serving tests for the local-client bridge."""

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import AsyncMock, MagicMock

import hermes_constants
from gateway import local_client_media
from gateway.local_client_bridge import LocalClientBridge, _build_app


TOKEN = "media-tok"

# 1x1 transparent PNG (valid header so mimetypes + real MIME sniffs match).
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    monkeypatch.setattr(local_client_media, "get_hermes_home", lambda: home)
    return home


def _fake_runner():
    runner = MagicMock()
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._background_tasks = set()
    runner._handle_message = AsyncMock(return_value=None)
    runner._interrupt_and_clear_session = AsyncMock(return_value=None)
    runner._session_key_for_source = lambda src: f"LOCAL:{src.chat_id}"
    runner._evict_cached_agent = MagicMock()
    return runner


def _make_bridge():
    bridge = LocalClientBridge(_fake_runner())
    bridge._token = TOKEN
    return bridge


def _write_media(home: Path, relpath: str, content: bytes = _PNG_BYTES) -> Path:
    target = home / "sessions" / "media" / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


@pytest.mark.asyncio
class TestMedia:
    async def test_serves_image_with_bearer(self, hermes_home):
        path = _write_media(hermes_home, "ok.png")
        async with TestClient(TestServer(_build_app(_make_bridge()))) as cli:
            resp = await cli.get(
                "/v1/local-client/media",
                params={"path": str(path)},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("image/")
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert await resp.read() == _PNG_BYTES

    async def test_serves_image_with_query_token(self, hermes_home):
        path = _write_media(hermes_home, "q.png")
        async with TestClient(TestServer(_build_app(_make_bridge()))) as cli:
            resp = await cli.get(
                "/v1/local-client/media",
                params={"path": str(path), "token": TOKEN},
            )
            assert resp.status == 200

    async def test_rejects_missing_token(self, hermes_home):
        path = _write_media(hermes_home, "ok.png")
        async with TestClient(TestServer(_build_app(_make_bridge()))) as cli:
            resp = await cli.get(
                "/v1/local-client/media", params={"path": str(path)}
            )
            assert resp.status == 401

    async def test_rejects_path_outside_root(self, hermes_home, tmp_path):
        (hermes_home / "sessions" / "media").mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "escaped.png"
        outside.write_bytes(_PNG_BYTES)
        async with TestClient(TestServer(_build_app(_make_bridge()))) as cli:
            resp = await cli.get(
                "/v1/local-client/media",
                params={"path": str(outside)},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status == 403

    async def test_rejects_traversal_literal(self, hermes_home):
        root = hermes_home / "sessions" / "media"
        root.mkdir(parents=True, exist_ok=True)
        traversal = str(root / ".." / ".." / ".." / "etc" / "passwd")
        async with TestClient(TestServer(_build_app(_make_bridge()))) as cli:
            resp = await cli.get(
                "/v1/local-client/media",
                params={"path": traversal},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status == 403

    async def test_rejects_non_image_extension(self, hermes_home):
        path = _write_media(hermes_home, "note.txt", content=b"hi")
        async with TestClient(TestServer(_build_app(_make_bridge()))) as cli:
            resp = await cli.get(
                "/v1/local-client/media",
                params={"path": str(path)},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status == 415

    async def test_missing_file_returns_404(self, hermes_home):
        root = hermes_home / "sessions" / "media"
        root.mkdir(parents=True, exist_ok=True)
        async with TestClient(TestServer(_build_app(_make_bridge()))) as cli:
            resp = await cli.get(
                "/v1/local-client/media",
                params={"path": str(root / "missing.png")},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status == 404

    async def test_missing_root_returns_503(self, hermes_home):
        # root not created intentionally
        async with TestClient(TestServer(_build_app(_make_bridge()))) as cli:
            resp = await cli.get(
                "/v1/local-client/media",
                params={"path": str(hermes_home / "sessions" / "media" / "x.png")},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status == 503

    async def test_missing_path_param_returns_400(self, hermes_home):
        async with TestClient(TestServer(_build_app(_make_bridge()))) as cli:
            resp = await cli.get(
                "/v1/local-client/media",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status == 400
