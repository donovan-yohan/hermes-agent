from pathlib import Path

import gateway.run as gateway_run
from gateway.run import GatewayRunner


def _make_runner():
    return object.__new__(GatewayRunner)


def test_extract_browser_bridge_image_attachments_accepts_remote_image(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    class _FakeResponse:
        def __init__(self):
            self.headers = {"Content-Type": "image/jpeg"}

        def read(self, _size=-1):
            return b"jpeg-bytes"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    captured = {}

    def _fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(gateway_run, "urlopen", _fake_urlopen)

    media_urls, media_types = GatewayRunner._extract_browser_bridge_image_attachments(
        _make_runner(),
        {
            "attachments": [
                {
                    "url": "https://media.discordapp.net/attachments/123/test.jpg",
                    "mime_type": "image/png",
                }
            ]
        },
    )

    assert captured == {
        "url": "https://media.discordapp.net/attachments/123/test.jpg",
        "timeout": 20,
    }
    assert media_types == ["photo"]
    assert len(media_urls) == 1
    saved_path = Path(media_urls[0])
    assert saved_path.exists()
    assert saved_path.suffix == ".jpg"
    assert saved_path.read_bytes() == b"jpeg-bytes"


def test_extract_browser_bridge_image_attachments_skips_oversized_remote_image(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    class _FakeResponse:
        def __init__(self):
            self.headers = {"Content-Type": "image/png"}

        def read(self, _size=-1):
            return b"x" * (12 * 1024 * 1024 + 1)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(gateway_run, "urlopen", lambda request, timeout=0: _FakeResponse())

    media_urls, media_types = GatewayRunner._extract_browser_bridge_image_attachments(
        _make_runner(),
        {
            "attachments": [
                {
                    "url": "https://cdn.discordapp.com/attachments/123/test.png",
                    "mime_type": "image/png",
                }
            ]
        },
    )

    assert media_urls == []
    assert media_types == []
