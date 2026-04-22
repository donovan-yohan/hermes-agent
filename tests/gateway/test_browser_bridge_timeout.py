import asyncio
import threading

import pytest

from gateway.browser_bridge import (
    BrowserBridgeConfig,
    BrowserBridgeRequestTimeout,
    BrowserBridgeServer,
)


def test_run_payload_returns_timeout_error_for_stalled_async_handler():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def _slow_handler(_payload):
        await asyncio.sleep(0.2)
        return {"ok": True}

    bridge = BrowserBridgeServer(
        loop=loop,
        handle_payload=_slow_handler,
        config=BrowserBridgeConfig(
            host="127.0.0.1",
            port=8765,
            token="test-token",
            request_timeout_seconds=0.01,
        ),
    )

    try:
        with pytest.raises(BrowserBridgeRequestTimeout, match="Browser bridge request exceeded"):
            bridge.run_payload({"message": "hello"})
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
