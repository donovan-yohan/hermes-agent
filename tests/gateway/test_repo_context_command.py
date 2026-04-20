"""Tests for gateway /repo command and repo-context persistence."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    return runner


def _make_source(*, chat_id: str = "thread-1", thread_id: str | None = "thread-1", parent_chat_id: str | None = "channel-1") -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        user_id="u1",
        chat_id=chat_id,
        chat_name="relay-ide / dev",
        chat_type="thread" if thread_id else "group",
        thread_id=thread_id,
        parent_chat_id=parent_chat_id,
        user_name="tester",
    )


def _make_event(text: str, *, source: SessionSource | None = None) -> MessageEvent:
    return MessageEvent(text=text, source=source or _make_source(), message_id="m1")


@pytest.mark.asyncio
async def test_repo_set_persists_thread_binding(monkeypatch, tmp_path):
    runner = _make_runner()
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    response = await runner._handle_repo_command(
        _make_event("/repo donovan-yohan/relay-ide path=~/src/relay-ide branch=main")
    )

    assert "relay-ide" in response
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    bindings = saved["repo_context_bindings"]
    assert bindings[0]["platform"] == "discord"
    assert bindings[0]["chat_id"] == "thread-1"
    assert bindings[0]["thread_id"] == "thread-1"
    assert bindings[0]["repo"] == "donovan-yohan/relay-ide"
    assert bindings[0]["local_path"] == "~/src/relay-ide"
    assert bindings[0]["default_branch"] == "main"


@pytest.mark.asyncio
async def test_repo_set_scope_chat_uses_parent_channel(monkeypatch, tmp_path):
    runner = _make_runner()
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    response = await runner._handle_repo_command(
        _make_event("/repo set donovan-yohan/relay-ide scope=chat")
    )

    assert "scope: chat" in response.lower()
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    bindings = saved["repo_context_bindings"]
    assert bindings[0]["chat_id"] == "channel-1"
    assert "thread_id" not in bindings[0]


@pytest.mark.asyncio
async def test_repo_show_uses_resolved_binding(monkeypatch, tmp_path):
    runner = _make_runner()
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "repo_context_bindings": [
                    {
                        "platform": "discord",
                        "chat_id": "channel-1",
                        "repo": "donovan-yohan/relay-ide",
                        "local_path": "~/src/relay-ide",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    response = await runner._handle_repo_command(_make_event("/repo show"))

    assert "donovan-yohan/relay-ide" in response
    assert "~/src/relay-ide" in response


@pytest.mark.asyncio
async def test_repo_clear_removes_binding(monkeypatch, tmp_path):
    runner = _make_runner()
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "repo_context_bindings": [
                    {
                        "platform": "discord",
                        "chat_id": "thread-1",
                        "thread_id": "thread-1",
                        "repo": "donovan-yohan/relay-ide",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    response = await runner._handle_repo_command(_make_event("/repo clear"))

    assert "cleared" in response.lower()
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8")) or {}
    assert saved.get("repo_context_bindings") == []
