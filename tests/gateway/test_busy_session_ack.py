"""Tests for busy-session acknowledgment when user sends messages during active agent runs.

Verifies that users get an immediate status response instead of total silence
when the agent is working on a task. See PR fix for the @Lonely__MH report.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal stubs so we can import gateway code without heavy deps
# ---------------------------------------------------------------------------
import sys, types

_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP = "supergroup"
_ct.GROUP = "group"
_ct.PRIVATE = "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SessionSource,
    build_session_key,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(text="hello", chat_id="123", platform_val="telegram"):
    """Build a minimal MessageEvent."""
    source = SessionSource(
        platform=MagicMock(value=platform_val),
        chat_id=chat_id,
        chat_type="private",
        user_id="user1",
    )
    evt = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg1",
    )
    return evt


def _make_runner():
    """Build a minimal GatewayRunner-like object for testing."""
    from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner._busy_input_mode = "queue"
    runner.adapters = {}
    runner.config = MagicMock()
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    return runner, _AGENT_PENDING_SENTINEL


def _make_adapter(platform_val="telegram"):
    """Build a minimal adapter mock."""
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = MagicMock(value=platform_val)
    return adapter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBusySessionAck:
    """User sends a message while agent is running — should get acknowledgment."""

    @pytest.mark.asyncio
    async def test_interrupt_mode_sends_ack_and_interrupts(self):
        """In interrupt mode, first message during busy session should get a status ack and interrupt."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="Are you working?")
        sk = build_session_key(event.source)

        # Simulate running agent
        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 21,
            "max_iterations": 60,
            "current_tool": "terminal",
            "last_activity_ts": time.time(),
            "last_activity_desc": "terminal",
            "seconds_since_activity": 1.0,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 600  # 10 min ago
        runner.adapters[event.source.platform] = adapter

        result = await runner._handle_active_session_busy_message(event, sk)

        assert result is True  # handled
        # Verify ack was sent
        adapter._send_with_retry.assert_called_once()
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        if not content and call_kwargs.args:
            # positional args
            content = str(call_kwargs)
        assert "Interrupting" in content or "respond" in content
        assert "/stop" not in content  # no need — we ARE interrupting

        # Verify message was queued in adapter pending
        assert sk in adapter._pending_messages

        # Verify agent interrupt was called
        agent.interrupt.assert_called_once_with("Are you working?")

    @pytest.mark.asyncio
    async def test_interrupt_mode_debounce_suppresses_rapid_acks(self):
        """In interrupt mode, second message within 30s should NOT send another ack but still interrupt."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event1 = _make_event(text="hello?")
        # Reuse the same source so platform mock matches
        event2 = MessageEvent(
            text="still there?",
            message_type=MessageType.TEXT,
            source=event1.source,
            message_id="msg2",
        )
        sk = build_session_key(event1.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 5,
            "max_iterations": 60,
            "current_tool": None,
            "last_activity_ts": time.time(),
            "last_activity_desc": "api_call",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 60
        runner.adapters[event1.source.platform] = adapter

        # First message — should get ack
        result1 = await runner._handle_active_session_busy_message(event1, sk)
        assert result1 is True
        assert adapter._send_with_retry.call_count == 1

        # Second message within cooldown — should be queued but no ack
        result2 = await runner._handle_active_session_busy_message(event2, sk)
        assert result2 is True
        assert adapter._send_with_retry.call_count == 1  # still 1, no new ack

        # But interrupt should still be called for both
        assert agent.interrupt.call_count == 2

    @pytest.mark.asyncio
    async def test_ack_after_cooldown_expires(self):
        """After 30s cooldown, a new message should send a fresh ack."""
        runner, sentinel = _make_runner()
        adapter = _make_adapter()

        event = _make_event(text="hello?")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 10,
            "max_iterations": 60,
            "current_tool": "web_search",
            "last_activity_ts": time.time(),
            "last_activity_desc": "tool",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 120
        runner.adapters[event.source.platform] = adapter

        # First ack
        await runner._handle_active_session_busy_message(event, sk)
        assert adapter._send_with_retry.call_count == 1

        # Fake that cooldown expired
        runner._busy_ack_ts[sk] = time.time() - 31

        # Second ack should go through
        await runner._handle_active_session_busy_message(event, sk)
        assert adapter._send_with_retry.call_count == 2

    @pytest.mark.asyncio
    async def test_interrupt_mode_includes_status_detail(self):
        """In interrupt mode, ack message should include iteration and tool info when available."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="yo")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 21,
            "max_iterations": 60,
            "current_tool": "terminal",
            "last_activity_ts": time.time(),
            "last_activity_desc": "terminal",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 600  # 10 min
        runner.adapters[event.source.platform] = adapter

        await runner._handle_active_session_busy_message(event, sk)

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")
        assert "21/60" in content  # iteration
        assert "terminal" in content  # current tool
        assert "10 min" in content  # elapsed

    @pytest.mark.asyncio
    async def test_draining_still_works(self):
        """Draining case should still produce the drain-specific message."""
        runner, sentinel = _make_runner()
        runner._draining = True
        adapter = _make_adapter()

        event = _make_event(text="hello")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        # Mock the drain-specific methods
        runner._queue_during_drain_enabled = lambda: False
        runner._status_action_gerund = lambda: "restarting"

        result = await runner._handle_active_session_busy_message(event, sk)
        assert result is True

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")
        assert "restarting" in content

    @pytest.mark.asyncio
    async def test_pending_sentinel_no_interrupt(self):
        """When agent is PENDING_SENTINEL, don't call interrupt (it has no method)."""
        runner, sentinel = _make_runner()
        adapter = _make_adapter()

        event = _make_event(text="hey")
        sk = build_session_key(event.source)

        runner._running_agents[sk] = sentinel
        runner._running_agents_ts[sk] = time.time()
        runner.adapters[event.source.platform] = adapter

        result = await runner._handle_active_session_busy_message(event, sk)
        assert result is True
        # Should still send ack
        adapter._send_with_retry.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_adapter_falls_through(self):
        """If adapter is missing, return False so default path handles it."""
        runner, sentinel = _make_runner()

        event = _make_event(text="hello")
        sk = build_session_key(event.source)

        # No adapter registered
        runner._running_agents[sk] = MagicMock()

        result = await runner._handle_active_session_busy_message(event, sk)
        assert result is False  # not handled, let default path try


class TestQueueModeBusySessionAck:
    """Queue mode (default): messages are queued, not interrupting."""

    @pytest.mark.asyncio
    async def test_queue_mode_sends_ack_and_no_interrupt(self):
        """In queue mode, message should be queued and agent NOT interrupted."""
        runner, sentinel = _make_runner()
        # runner defaults to queue already
        adapter = _make_adapter()

        event = _make_event(text="Follow-up question")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 21,
            "max_iterations": 60,
            "current_tool": "terminal",
            "last_activity_ts": time.time(),
            "last_activity_desc": "terminal",
            "seconds_since_activity": 1.0,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 600
        runner.adapters[event.source.platform] = adapter

        result = await runner._handle_active_session_busy_message(event, sk)

        assert result is True
        # Verify ack was sent with queue wording
        adapter._send_with_retry.assert_called_once()
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")
        assert "Queued" in content or "queued" in content or "next turn" in content

        # Verify message was queued in adapter pending
        assert sk in adapter._pending_messages

        # Verify agent was NOT interrupted
        agent.interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_queue_mode_debounce_suppresses_rapid_acks(self):
        """In queue mode, second message within 30s should NOT send another ack."""
        runner, sentinel = _make_runner()
        adapter = _make_adapter()

        event1 = _make_event(text="hello?")
        event2 = MessageEvent(
            text="still there?",
            message_type=MessageType.TEXT,
            source=event1.source,
            message_id="msg2",
        )
        sk = build_session_key(event1.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 5,
            "max_iterations": 60,
            "current_tool": None,
            "last_activity_ts": time.time(),
            "last_activity_desc": "api_call",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 60
        runner.adapters[event1.source.platform] = adapter

        # First message — should get ack
        result1 = await runner._handle_active_session_busy_message(event1, sk)
        assert result1 is True
        assert adapter._send_with_retry.call_count == 1

        # Second message within cooldown — should be queued but no ack
        result2 = await runner._handle_active_session_busy_message(event2, sk)
        assert result2 is True
        assert adapter._send_with_retry.call_count == 1  # still 1, no new ack

        # Agent should NOT be interrupted in queue mode
        agent.interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_queue_mode_ack_after_cooldown_expires(self):
        """In queue mode, after 30s cooldown a new message should send a fresh ack."""
        runner, sentinel = _make_runner()
        adapter = _make_adapter()

        event = _make_event(text="hello?")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 10,
            "max_iterations": 60,
            "current_tool": "web_search",
            "last_activity_ts": time.time(),
            "last_activity_desc": "tool",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 120
        runner.adapters[event.source.platform] = adapter

        # First ack
        await runner._handle_active_session_busy_message(event, sk)
        assert adapter._send_with_retry.call_count == 1

        # Fake that cooldown expired
        runner._busy_ack_ts[sk] = time.time() - 31

        # Second ack should go through
        await runner._handle_active_session_busy_message(event, sk)
        assert adapter._send_with_retry.call_count == 2

        # Agent should NOT be interrupted in queue mode
        agent.interrupt.assert_not_called()


class TestSteerModeBusySessionAck:
    """Steer mode: messages are injected into the current turn via agent.steer()."""

    @pytest.mark.asyncio
    async def test_steer_mode_calls_steer_no_interrupt_no_pending(self):
        """In steer mode, message text is delivered via agent.steer() and not queued."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()

        event = _make_event(text="please use ripgrep instead")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.steer.return_value = True
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 30
        runner.adapters[event.source.platform] = adapter

        result = await runner._handle_active_session_busy_message(event, sk)

        assert result is True
        agent.steer.assert_called_once_with("please use ripgrep instead")
        agent.interrupt.assert_not_called()
        # Steer must NOT enqueue a pending next-turn message — otherwise the
        # text would be delivered twice (once mid-run, once as next turn).
        assert sk not in adapter._pending_messages

        adapter._send_with_retry.assert_called_once()
        content = adapter._send_with_retry.call_args.kwargs.get("content", "")
        assert "Steering" in content or "steer" in content.lower()

    @pytest.mark.asyncio
    async def test_steer_mode_falls_back_to_queue_for_photo(self):
        """Photo events can't be steered (text-only); should fall back to queue."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()

        event = MessageEvent(
            text="caption",
            message_type=MessageType.PHOTO,
            source=_make_event().source,
            message_id="msg-photo",
        )
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 1, "max_iterations": 60, "current_tool": None,
            "last_activity_ts": time.time(), "last_activity_desc": "",
            "seconds_since_activity": 0.0,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time()
        runner.adapters[event.source.platform] = adapter

        result = await runner._handle_active_session_busy_message(event, sk)

        assert result is True
        agent.steer.assert_not_called()
        agent.interrupt.assert_not_called()
        # Queue fallback: pending message stored, queue ack sent
        assert sk in adapter._pending_messages
        content = adapter._send_with_retry.call_args.kwargs.get("content", "")
        assert "Queued" in content or "queued" in content

    @pytest.mark.asyncio
    async def test_steer_mode_falls_back_to_queue_when_agent_pending(self):
        """If running_agent is the PENDING_SENTINEL we can't steer — queue instead."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()

        event = _make_event(text="nudge")
        sk = build_session_key(event.source)

        runner._running_agents[sk] = sentinel
        runner._running_agents_ts[sk] = time.time()
        runner.adapters[event.source.platform] = adapter

        result = await runner._handle_active_session_busy_message(event, sk)

        assert result is True
        # Sentinel has no .steer / .interrupt to call. We just verify queue path ran.
        assert sk in adapter._pending_messages
        content = adapter._send_with_retry.call_args.kwargs.get("content", "")
        assert "Queued" in content or "queued" in content

    @pytest.mark.asyncio
    async def test_steer_mode_falls_back_when_steer_returns_false(self):
        """If agent.steer() refuses (e.g. empty after strip), queue instead."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()

        event = _make_event(text="something")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.steer.return_value = False
        agent.get_activity_summary.return_value = {
            "api_call_count": 1, "max_iterations": 60, "current_tool": None,
            "last_activity_ts": time.time(), "last_activity_desc": "",
            "seconds_since_activity": 0.0,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time()
        runner.adapters[event.source.platform] = adapter

        result = await runner._handle_active_session_busy_message(event, sk)

        assert result is True
        agent.steer.assert_called_once()
        agent.interrupt.assert_not_called()
        assert sk in adapter._pending_messages


class TestLoadBusyInputMode:
    def test_loads_steer_from_env(self, monkeypatch):
        from gateway.run import GatewayRunner
        monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "steer")
        assert GatewayRunner._load_busy_input_mode() == "steer"

    def test_loads_interrupt_from_env(self, monkeypatch):
        from gateway.run import GatewayRunner
        monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "interrupt")
        assert GatewayRunner._load_busy_input_mode() == "interrupt"

    def test_unknown_mode_falls_back_to_queue(self, monkeypatch):
        from gateway.run import GatewayRunner
        monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "bogus")
        assert GatewayRunner._load_busy_input_mode() == "queue"
