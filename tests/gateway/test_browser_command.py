"""Tests for /browser gateway slash command handling."""

import inspect
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


if "firecrawl" not in sys.modules:
    firecrawl_stub = types.ModuleType("firecrawl")
    firecrawl_stub.Firecrawl = object
    sys.modules["firecrawl"] = firecrawl_stub

if "fal_client" not in sys.modules:
    fal_client_stub = types.ModuleType("fal_client")
    sys.modules["fal_client"] = fal_client_stub


def _make_event(text="/browser status", platform=Platform.LOCAL, user_id="local-user", chat_id="local-chat"):
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="tester",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner.session_store = MagicMock()

    from gateway.hooks import HookRegistry

    runner.hooks = HookRegistry()
    return runner


@pytest.mark.asyncio
async def test_help_output_includes_browser_command():
    runner = _make_runner()
    event = _make_event(text="/help")
    result = await runner._handle_help_command(event)
    assert "/browser [connect|disconnect|status]" in result


def test_browser_is_known_gateway_command():
    from gateway.run import GatewayRunner

    source = inspect.getsource(GatewayRunner._handle_message)
    assert '"browser"' in source


@pytest.mark.asyncio
async def test_browser_connect_status_disconnect_round_trip():
    runner = _make_runner()
    remote_cdp = "wss://example.com/devtools/browser/abc"

    with (
        patch.dict(os.environ, {}, clear=False),
        patch("tools.browser_tool.read_shared_cdp_state", return_value={}),
        patch("tools.browser_tool.persist_shared_cdp_state", return_value=True) as persist_state,
        patch("tools.browser_tool.clear_shared_cdp_state") as clear_state,
        patch("tools.browser_tool.cleanup_all_browsers") as cleanup,
    ):
        connect_event = _make_event(text=f"/browser connect {remote_cdp}")
        connect_result = await runner._handle_browser_command(connect_event)
        assert "connected to live CDP" in connect_result
        assert remote_cdp in connect_result
        assert os.environ.get("BROWSER_CDP_URL") == remote_cdp
        persist_state.assert_called_once()

        status_event = _make_event(text="/browser status")
        status_result = await runner._handle_browser_command(status_event)
        assert "connected to live CDP" in status_result
        assert remote_cdp in status_result

        disconnect_event = _make_event(text="/browser disconnect")
        disconnect_result = await runner._handle_browser_command(disconnect_event)
        assert "disconnected from live CDP" in disconnect_result
        clear_state.assert_called_once()
        cleanup.assert_called()
