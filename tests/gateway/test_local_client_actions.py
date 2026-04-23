"""Action-dispatch tests for the local-client session service."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform
from gateway.local_client_bridge import LocalClientBridge, _build_app
from gateway.local_client_sessions import (
    LocalClientSessionService,
    _chat_id_prefix,
    _slug,
    build_local_source,
)
from gateway.local_client_bridge import LocalClientIdentity, LocalClientRequest


TOKEN = "tok"
CLIENT = {"kind": "browser-sidecar", "label": "Chrome", "client_session_id": "panel-1"}


def _fake_runner(running=None, transcript=None, entries=None, sessions=None):
    runner = MagicMock()
    runner.session_store = MagicMock()
    runner.session_store.list_sessions.return_value = sessions or []
    runner.session_store._entries = entries or {}
    runner.session_store.load_transcript.return_value = transcript or []
    runner.session_store.reset_session = MagicMock()
    runner._running_agents = running if running is not None else {}
    runner._background_tasks = set()
    runner._handle_message = AsyncMock(return_value=None)
    runner._interrupt_and_clear_session = AsyncMock(return_value=None)
    runner._session_key_for_source = lambda src: f"LOCAL:{src.chat_id}"
    runner._evict_cached_agent = MagicMock()
    runner._invalidate_session_run_generation = MagicMock()
    return runner


def _make_bridge(runner):
    bridge = LocalClientBridge(runner)
    bridge._token = TOKEN
    return bridge


def _body(action, message=None, context=None, client=None):
    body = {"client": client or dict(CLIENT), "action": action}
    if message is not None:
        body["message"] = message
    if context is not None:
        body["context"] = context
    return body


async def _post(cli, body):
    return await cli.post(
        "/v1/local-client/request",
        json=body,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


# --------------------------------------------------------------------------- #
# build_local_source + slug invariants
# --------------------------------------------------------------------------- #


class TestSourceShape:
    def test_platform_is_local(self):
        src = build_local_source(LocalClientIdentity("browser-sidecar", "Chrome Ext", "panel-1"))
        assert src.platform == Platform.LOCAL

    def test_chat_id_format(self):
        src = build_local_source(LocalClientIdentity("k", "My Label!", "SESS 42"))
        assert src.chat_id == "local-client:my-label:sess-42"

    def test_user_id_has_label_only(self):
        src = build_local_source(LocalClientIdentity("k", "Chrome", "panel-1"))
        assert src.user_id == "local-client:chrome"

    def test_empty_pieces_default(self):
        src = build_local_source(LocalClientIdentity("k", "", ""))
        assert src.chat_id == "local-client:unknown:default"

    def test_chat_id_prefix_matches(self):
        assert _chat_id_prefix("Chrome Ext") == "local-client:chrome-ext:"


# --------------------------------------------------------------------------- #
# Action dispatch (via aiohttp)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestSend:
    async def test_send_awaits_handle_message_with_local_source(self):
        runner = _fake_runner()
        bridge = _make_bridge(runner)
        async with TestClient(TestServer(_build_app(bridge))) as cli:
            resp = await _post(cli, _body("send", message="hello"))
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["accepted"] is True
            assert data["session_key"].startswith("LOCAL:local-client:chrome:")

            runner._handle_message.assert_awaited_once()
            (event,), _ = runner._handle_message.call_args
            assert event.text == "hello"
            assert event.source.platform == Platform.LOCAL
            assert event.source.chat_id.startswith("local-client:chrome:")
            assert event.internal is False

    async def test_send_without_message_400s_at_dispatch(self):
        runner = _fake_runner()
        async with TestClient(TestServer(_build_app(_make_bridge(runner)))) as cli:
            resp = await _post(cli, _body("send"))
            assert resp.status == 200  # dispatch surfaces structured error
            data = await resp.json()
            assert data["ok"] is False
            assert "message" in data["error"]
            runner._handle_message.assert_not_awaited()

    async def test_send_async_schedules_task_and_returns_immediately(self):
        runner = _fake_runner()
        # Make the handler slow so the response returns before completion.
        slow = asyncio.Event()

        async def _slow(event):
            await slow.wait()

        runner._handle_message = AsyncMock(side_effect=_slow)
        async with TestClient(TestServer(_build_app(_make_bridge(runner)))) as cli:
            resp = await _post(cli, _body("send_async", message="bg"))
            data = await resp.json()
            assert resp.status == 200
            assert data["accepted"] is True
            assert data["running"] is True
            # Task is tracked.
            assert any(not t.done() for t in runner._background_tasks)
            # Allow the scheduled task to reach wait() before releasing it.
            await asyncio.sleep(0)
            slow.set()
            # Drain
            for task in list(runner._background_tasks):
                try:
                    await task
                except Exception:
                    pass

    async def test_busy_guard_blocks_new_send(self):
        runner = _fake_runner(running={"LOCAL:local-client:chrome:panel-1": object()})
        async with TestClient(TestServer(_build_app(_make_bridge(runner)))) as cli:
            resp = await _post(cli, _body("send", message="hi"))
            data = await resp.json()
            assert resp.status == 200
            assert data["accepted"] is False
            assert data["busy"] is True
            runner._handle_message.assert_not_awaited()


@pytest.mark.asyncio
class TestStateListResetInterrupt:
    async def test_state_without_entry_returns_empty_messages(self):
        runner = _fake_runner()
        async with TestClient(TestServer(_build_app(_make_bridge(runner)))) as cli:
            resp = await _post(cli, _body("state"))
            data = await resp.json()
            assert data["messages"] == []
            assert data["running"] is False

    async def test_state_projects_transcript(self):
        entry = SimpleNamespace(session_id="sess123")
        runner = _fake_runner(
            entries={"LOCAL:local-client:chrome:panel-1": entry},
            transcript=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello", "tool_name": None},
                {"role": "system", "content": "note"},
                # Discord-thread markers and similar product fields must be
                # stripped — only the projected keys survive.
                {"role": "user", "content": "x", "discord_thread_id": "abc"},
                {"role": "debug", "content": "ignored"},  # filtered
            ],
        )
        async with TestClient(TestServer(_build_app(_make_bridge(runner)))) as cli:
            resp = await _post(cli, _body("state"))
            data = await resp.json()
            assert len(data["messages"]) == 4
            for m in data["messages"]:
                assert set(m.keys()) == {"role", "content", "tool_name"}

    async def test_list_filters_by_platform_and_prefix(self):
        def _entry(platform, chat_id, key, sid):
            return SimpleNamespace(
                session_key=key,
                session_id=sid,
                updated_at=datetime(2026, 4, 22, 12, 0, 0),
                platform=platform,
                origin=SimpleNamespace(chat_id=chat_id),
            )

        sessions = [
            _entry(Platform.LOCAL, "local-client:chrome:panel-1", "k1", "s1"),
            _entry(Platform.LOCAL, "local-client:firefox:panel-2", "k2", "s2"),
            _entry(Platform.TELEGRAM, "local-client:chrome:panel-3", "k3", "s3"),
            _entry(Platform.LOCAL, "unrelated", "k4", "s4"),
        ]
        runner = _fake_runner(sessions=sessions)
        async with TestClient(TestServer(_build_app(_make_bridge(runner)))) as cli:
            resp = await _post(cli, _body("list"))
            data = await resp.json()
            assert [s["session_key"] for s in data["sessions"]] == ["k1"]

    async def test_reset_calls_store_and_evicts(self):
        runner = _fake_runner()
        async with TestClient(TestServer(_build_app(_make_bridge(runner)))) as cli:
            resp = await _post(cli, _body("reset"))
            assert resp.status == 200
            runner.session_store.reset_session.assert_called_once()
            key = runner.session_store.reset_session.call_args.args[0]
            assert key.startswith("LOCAL:local-client:chrome:")
            runner._evict_cached_agent.assert_called_once_with(key)
            runner._invalidate_session_run_generation.assert_called_once_with(key, reason="session_reset")

    async def test_interrupt_calls_gateway_with_expected_kwargs(self):
        runner = _fake_runner()
        async with TestClient(TestServer(_build_app(_make_bridge(runner)))) as cli:
            resp = await _post(cli, _body("interrupt"))
            data = await resp.json()
            assert data["interrupt_requested"] is True
            runner._interrupt_and_clear_session.assert_awaited_once()
            call = runner._interrupt_and_clear_session.call_args
            assert call.kwargs["release_running_state"] is True
            assert call.kwargs["interrupt_reason"]
            assert call.kwargs["invalidation_reason"]
