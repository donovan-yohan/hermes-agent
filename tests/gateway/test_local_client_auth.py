"""Auth/transport tests for the local-client bridge."""

import pytest
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import AsyncMock, MagicMock

from gateway.config import Platform
from gateway.local_client_bridge import (
    AUTH_HEADER_ALT,
    LocalClientBridge,
    _build_app,
)


TOKEN = "test-tok"


def _fake_runner():
    runner = MagicMock()
    runner.session_store = MagicMock()
    runner.session_store.list_sessions.return_value = []
    runner.session_store._entries = {}
    runner.session_store.load_transcript.return_value = []
    runner.session_store.reset_session = MagicMock()
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


def _body(action="state"):
    return {
        "client": {"kind": "browser-sidecar", "label": "Test", "client_session_id": "a"},
        "action": action,
    }


@pytest.mark.asyncio
class TestHealth:
    async def test_health_open_no_auth_required(self):
        bridge = _make_bridge()
        async with TestClient(TestServer(_build_app(bridge))) as cli:
            resp = await cli.get("/health")
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True, "service": "local-client-bridge"}


@pytest.mark.asyncio
class TestRequestAuth:
    async def test_rejects_missing_token(self):
        bridge = _make_bridge()
        async with TestClient(TestServer(_build_app(bridge))) as cli:
            resp = await cli.post("/v1/local-client/request", json=_body())
            assert resp.status == 401

    async def test_rejects_wrong_bearer(self):
        bridge = _make_bridge()
        async with TestClient(TestServer(_build_app(bridge))) as cli:
            resp = await cli.post(
                "/v1/local-client/request",
                json=_body(),
                headers={"Authorization": "Bearer nope"},
            )
            assert resp.status == 401

    async def test_rejects_wrong_same_length_bearer(self):
        bridge = _make_bridge()
        wrong = "x" * len(TOKEN)
        async with TestClient(TestServer(_build_app(bridge))) as cli:
            resp = await cli.post(
                "/v1/local-client/request",
                json=_body(),
                headers={"Authorization": f"Bearer {wrong}"},
            )
            assert resp.status == 401

    async def test_accepts_correct_bearer(self):
        bridge = _make_bridge()
        async with TestClient(TestServer(_build_app(bridge))) as cli:
            resp = await cli.post(
                "/v1/local-client/request",
                json=_body(),
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True

    async def test_accepts_alt_header(self):
        bridge = _make_bridge()
        async with TestClient(TestServer(_build_app(bridge))) as cli:
            resp = await cli.post(
                "/v1/local-client/request",
                json=_body(),
                headers={AUTH_HEADER_ALT: TOKEN},
            )
            assert resp.status == 200

    async def test_invalid_json_body_returns_400(self):
        bridge = _make_bridge()
        async with TestClient(TestServer(_build_app(bridge))) as cli:
            resp = await cli.post(
                "/v1/local-client/request",
                data="not json",
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            assert resp.status == 400

    async def test_schema_error_returns_400(self):
        bridge = _make_bridge()
        async with TestClient(TestServer(_build_app(bridge))) as cli:
            resp = await cli.post(
                "/v1/local-client/request",
                json={"action": "state"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["ok"] is False
            assert "client" in body["error"].lower()


@pytest.mark.asyncio
class TestEnabledGate:
    async def test_enabled_false_when_no_token_and_no_env(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HERMES_LOCAL_CLIENT_ENABLED", raising=False)
        monkeypatch.delenv("HERMES_LOCAL_CLIENT_TOKEN", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        bridge = LocalClientBridge(_fake_runner())
        assert bridge.enabled is False

    async def test_enabled_true_with_env_token(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HERMES_LOCAL_CLIENT_ENABLED", raising=False)
        monkeypatch.setenv("HERMES_LOCAL_CLIENT_TOKEN", "x")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        bridge = LocalClientBridge(_fake_runner())
        assert bridge.enabled is True

    async def test_enabled_true_with_enabled_flag(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HERMES_LOCAL_CLIENT_TOKEN", raising=False)
        monkeypatch.setenv("HERMES_LOCAL_CLIENT_ENABLED", "1")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        bridge = LocalClientBridge(_fake_runner())
        assert bridge.enabled is True
