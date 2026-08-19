"""Behavioural tests for the external-participant publisher seam.

Everything here goes through the public module API (``tui_gateway.participants``)
and observes only public surfaces: the durable SQLite transcript, the gateway's
own history projection, the emitted event frames, and the shared helpers that
decide what counts as a real user turn.
"""

from __future__ import annotations

import importlib
import sys
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture()
def server(hermes_home):
    with patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
    # patch.dict restores sys.modules on exit, which would evict the module we
    # just imported — the publisher resolves the gateway by importing it, and
    # must land on THIS object, not a second copy with an empty session table.
    sys.modules["tui_gateway.server"] = mod
    methods = dict(mod._methods)
    yield mod
    mod._methods.clear()
    mod._methods.update(methods)
    mod._sessions.clear()
    mod._db = None


@pytest.fixture()
def participants(server):
    mod = importlib.import_module("tui_gateway.participants")
    yield mod
    with mod._registry_lock:
        mod._rosters.clear()
        mod._active_turns.clear()


@pytest.fixture()
def db(hermes_home):
    handle = SessionDB(db_path=hermes_home / "state.db")
    yield handle
    handle.close()


@pytest.fixture()
def events(server, monkeypatch):
    frames: list[dict] = []
    monkeypatch.setattr(server, "write_json", lambda frame: frames.append(frame) or True)
    return frames


def _make_session(server, db, sid: str, key: str) -> dict:
    db.create_session(key, source="tui")
    session = {
        "session_key": key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "agent": MagicMock(),
        "attached_images": [],
        "cols": 120,
        "source": "tui",
    }
    server._sessions[sid] = session
    server._db = db
    return session


CLAUDE = {
    "id": "claude:default",
    "handle": "claude",
    "display_name": "Claude Code",
    "adapter_id": "claude-code-stream-json",
    "status": "ready",
    "capabilities": {"text": True, "streaming": True},
}


@pytest.fixture()
def live(server, participants, db):
    sid, key = "gw-participants", "durable-participants"
    session = _make_session(server, db, sid, key)
    participants.register_participants(sid, "plugin-relay", [CLAUDE])
    return sid, key, session


def _payloads(frames, event_type):
    return [
        frame["params"]["payload"]
        for frame in frames
        if frame.get("params", {}).get("type") == event_type
    ]


# ── roster ──────────────────────────────────────────────────────────────────


def test_register_is_an_idempotent_upsert(live, participants):
    sid, _key, _session = live
    participants.register_participants(sid, "plugin-relay", [CLAUDE])
    participants.register_participants(
        sid, "plugin-relay", [dict(CLAUDE, status="busy", display_name="Claude")]
    )

    roster = participants.list_participants(sid)
    assert len(roster) == 1
    assert roster[0]["id"] == "claude:default"
    assert roster[0]["status"] == "busy"
    assert roster[0]["display_name"] == "Claude"
    assert roster[0]["plugin_id"] == "plugin-relay"
    assert roster[0]["capabilities"] == {"text": True, "streaming": True}


@pytest.mark.parametrize(
    "handle",
    ["", "cla ude", "Claude!", "-leading", "@@", "x" * 33],
)
def test_registration_rejects_an_unusable_handle(live, participants, handle):
    sid, _key, _session = live
    with pytest.raises(participants.ParticipantSeamError):
        participants.register_participants(
            sid, "plugin-relay", [dict(CLAUDE, id="other", handle=handle)]
        )


def test_handles_and_mentions_normalize_the_same_way(live, participants, db, events):
    sid, key, _session = live
    participants.register_participants(
        sid, "plugin-relay", [dict(CLAUDE, id="upper", handle="@Codex")]
    )
    assert [p["handle"] for p in participants.list_participants(sid)] == ["claude", "codex"]

    participants.append_participant_user_message(sid, "plugin-relay", "hi", ["@Codex"])
    assert db.get_messages(key)[0]["display_metadata"]["mentions"] == ["codex"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "i" * 129},
        {"adapter_id": "a" * 129},
        {"display_name": "d" * 65},
    ],
)
def test_registration_rejects_overlong_identity_fields(live, participants, overrides):
    sid, _key, _session = live
    entry = dict(CLAUDE, id="bounded", handle="bounded")
    entry.update(overrides)
    with pytest.raises(participants.ParticipantSeamError):
        participants.register_participants(sid, "plugin-relay", [entry])


def test_registration_rejects_an_overlong_plugin_id(live, participants):
    sid, _key, _session = live
    with pytest.raises(participants.ParticipantSeamError):
        participants.register_participants(sid, "p" * 129, [CLAUDE])


def test_control_characters_are_stripped_from_a_display_name(live, participants, db):
    sid, key, _session = live
    participants.register_participants(
        sid,
        "plugin-relay",
        [dict(CLAUDE, id="ctl", handle="ctl", display_name="Line\nBreak\x00")],
    )
    participants.begin_participant_message(sid, "plugin-relay", "ctl", "pt-ctl")
    stored = db.get_messages(key)[0]["display_metadata"]["participant"]["display_name"]
    assert "\n" not in stored
    assert "\x00" not in stored
    assert stored == "Line Break"


def test_a_second_plugin_cannot_claim_a_registered_participant(live, participants):
    sid, _key, _session = live
    with pytest.raises(participants.OwnershipError):
        participants.register_participants(sid, "other-plugin", [CLAUDE])
    # The rejected batch is not partially applied.
    assert [p["plugin_id"] for p in participants.list_participants(sid)] == ["plugin-relay"]


def test_a_second_plugin_cannot_claim_a_registered_handle(live, participants):
    """A mention names a handle, so two plugins cannot both answer to one."""
    sid, _key, _session = live
    with pytest.raises(participants.OwnershipError):
        participants.register_participants(
            sid, "other-plugin", [dict(CLAUDE, id="other:default", handle="claude")]
        )
    assert [p["id"] for p in participants.list_participants(sid)] == ["claude:default"]

    # A distinct handle from another plugin is fine.
    participants.register_participants(
        sid, "other-plugin", [dict(CLAUDE, id="other:default", handle="codex")]
    )
    assert {p["handle"] for p in participants.list_participants(sid)} == {"claude", "codex"}


def test_the_owning_plugin_may_re_register_its_own_handle(live, participants):
    sid, _key, _session = live
    participants.register_participants(sid, "plugin-relay", [dict(CLAUDE, status="busy")])
    roster = participants.list_participants(sid)
    assert [p["handle"] for p in roster] == ["claude"]
    assert roster[0]["status"] == "busy"


def test_unknown_session_is_typed(server, participants, db):
    with pytest.raises(participants.UnknownSessionError):
        participants.register_participants("nope", "plugin-relay", [CLAUDE])
    with pytest.raises(participants.UnknownSessionError):
        participants.list_participants("nope")
    with pytest.raises(participants.UnknownSessionError):
        participants.append_participant_user_message("nope", "plugin-relay", "hi", ["claude"])
    with pytest.raises(participants.UnknownSessionError):
        participants.begin_participant_message("nope", "plugin-relay", "claude:default", "t1")


def test_roster_is_forgotten_when_the_session_goes_away(live, participants, server):
    sid, _key, _session = live
    server._sessions.pop(sid)
    with pytest.raises(participants.UnknownSessionError):
        participants.list_participants(sid)
    with participants._registry_lock:
        assert sid not in participants._rosters


# ── participant-directed human row ──────────────────────────────────────────


def test_user_message_persists_projects_and_emits(live, participants, server, db, events):
    sid, key, session = live

    row_id = participants.append_participant_user_message(
        sid, "plugin-relay", "@claude summarise the diff", ["@claude"]
    )

    row = [r for r in db.get_messages(key) if r["id"] == row_id][0]
    assert row["role"] == "user"
    assert row["content"] == "@claude summarise the diff"
    assert row["display_kind"] == "participant_directed"
    assert row["display_metadata"] == {"mentions": ["claude"], "plugin_id": "plugin-relay"}

    projected = server._history_to_messages(session["history"])
    assert projected == [
        {
            "role": "user",
            "text": "@claude summarise the diff",
            "timestamp": pytest.approx(session["history"][0]["timestamp"]),
            "row_id": row_id,
            "display_kind": "participant_directed",
            "display_metadata": {"mentions": ["claude"], "plugin_id": "plugin-relay"},
        }
    ]

    payloads = _payloads(events, "participant.user_message")
    assert len(payloads) == 1
    assert payloads[0]["row_id"] == row_id
    assert payloads[0]["text"] == "@claude summarise the diff"
    assert payloads[0]["mentions"] == ["claude"]
    assert isinstance(payloads[0]["timestamp"], float)


def test_user_message_rejects_empty_text(live, participants):
    sid, _key, _session = live
    with pytest.raises(participants.ParticipantSeamError):
        participants.append_participant_user_message(sid, "plugin-relay", "   ", [])
    with pytest.raises(participants.ParticipantSeamError):
        participants.append_participant_user_message(sid, "plugin-relay", "hi", "claude")


# ── streamed participant reply ──────────────────────────────────────────────


def test_streamed_reply_lands_in_db_history_and_events(live, participants, server, db, events):
    sid, key, session = live

    row_id = participants.begin_participant_message(
        sid, "plugin-relay", "claude:default", "pturn-1"
    )
    opened = [r for r in db.get_messages(key) if r["id"] == row_id][0]
    assert opened["role"] == "assistant"
    assert opened["content"] == ""
    assert opened["display_kind"] == "participant_message"
    assert opened["display_metadata"]["status"] == "streaming"
    assert opened["display_metadata"]["participant"] == {
        "id": "claude:default",
        "handle": "claude",
        "display_name": "Claude Code",
        "plugin_id": "plugin-relay",
        "adapter_id": "claude-code-stream-json",
    }

    participants.append_participant_delta(sid, "plugin-relay", "pturn-1", "Hello ")
    participants.append_participant_delta(sid, "plugin-relay", "pturn-1", "world")
    # Deltas never touch the transcript.
    assert [r for r in db.get_messages(key) if r["id"] == row_id][0]["content"] == ""

    participants.complete_participant_message(sid, "plugin-relay", "pturn-1")

    final = [r for r in db.get_messages(key) if r["id"] == row_id][0]
    assert final["content"] == "Hello world"
    assert final["display_metadata"]["status"] == "completed"
    assert "error" not in final["display_metadata"]

    entry = session["history"][0]
    assert entry["role"] == "assistant"
    assert entry["content"] == "Hello world"
    assert entry["display_metadata"]["status"] == "completed"

    projected = server._history_to_messages(session["history"])
    assert len(projected) == 1
    assert projected[0]["role"] == "assistant"
    assert projected[0]["text"] == "Hello world"
    assert projected[0]["display_kind"] == "participant_message"
    assert projected[0]["display_metadata"]["participant"]["handle"] == "claude"
    assert projected[0]["row_id"] == row_id

    start = _payloads(events, "participant.message.start")
    assert len(start) == 1
    assert start[0]["participant_turn_id"] == "pturn-1"
    assert start[0]["row_id"] == row_id
    assert start[0]["participant"]["display_name"] == "Claude Code"
    assert isinstance(start[0]["timestamp"], float)

    deltas = _payloads(events, "participant.message.delta")
    assert [d["text"] for d in deltas] == ["Hello ", "world"]
    assert all(d == {"participant_turn_id": "pturn-1", "row_id": row_id, "text": d["text"]} for d in deltas)

    done = _payloads(events, "participant.message.complete")
    assert len(done) == 1
    assert done[0]["participant_turn_id"] == "pturn-1"
    assert done[0]["row_id"] == row_id
    assert done[0]["status"] == "completed"
    assert done[0]["text"] == "Hello world"
    assert "error" not in done[0]


def test_explicit_text_wins_over_the_delta_buffer(live, participants, db, events):
    sid, key, _session = live
    row_id = participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")
    participants.append_participant_delta(sid, "plugin-relay", "pt", "partial")
    participants.complete_participant_message(
        sid, "plugin-relay", "pt", status="completed", text="canonical answer"
    )

    assert [r for r in db.get_messages(key) if r["id"] == row_id][0]["content"] == "canonical answer"
    assert _payloads(events, "participant.message.complete")[0]["text"] == "canonical answer"


def test_failed_status_records_the_error(live, participants, db, events):
    sid, key, session = live
    row_id = participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")
    participants.append_participant_delta(sid, "plugin-relay", "pt", "half an answer")
    participants.complete_participant_message(
        sid, "plugin-relay", "pt", status="failed", error="adapter exited 1"
    )

    row = [r for r in db.get_messages(key) if r["id"] == row_id][0]
    assert row["display_metadata"]["status"] == "failed"
    assert row["display_metadata"]["error"] == "adapter exited 1"
    assert row["content"] == "half an answer"
    assert session["history"][0]["display_metadata"]["error"] == "adapter exited 1"

    done = _payloads(events, "participant.message.complete")[0]
    assert done["status"] == "failed"
    assert done["error"] == "adapter exited 1"


def test_completing_clears_the_active_turn(live, participants):
    sid, _key, _session = live
    participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")
    participants.complete_participant_message(sid, "plugin-relay", "pt")
    with pytest.raises(participants.UnknownTurnError):
        participants.append_participant_delta(sid, "plugin-relay", "pt", "late")
    with pytest.raises(participants.UnknownTurnError):
        participants.complete_participant_message(sid, "plugin-relay", "pt")


def test_a_turn_id_cannot_be_opened_twice(live, participants):
    sid, _key, _session = live
    participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")
    with pytest.raises(participants.ParticipantSeamError):
        participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")


def test_concurrent_begins_publish_exactly_one_row(live, participants, db):
    sid, key, _session = live
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def _begin():
        barrier.wait(timeout=10)
        try:
            participants.begin_participant_message(sid, "plugin-relay", "claude:default", "race")
            result = "ok"
        except participants.ParticipantSeamError:
            result = "rejected"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=_begin) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcomes) == ["ok", "rejected"]
    rows = [r for r in db.get_messages(key) if r["display_kind"] == "participant_message"]
    assert len(rows) == 1


def test_a_failed_begin_releases_the_turn_id_for_a_retry(live, participants, db, monkeypatch):
    sid, key, _session = live
    original = db.append_message

    def _boom(*args, **kwargs):
        raise RuntimeError("disk gremlin")

    monkeypatch.setattr(db, "append_message", _boom)
    with pytest.raises(participants.ParticipantSeamError):
        participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")

    monkeypatch.setattr(db, "append_message", original)
    row_id = participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")
    assert [r["id"] for r in db.get_messages(key)] == [row_id]


def test_a_late_delta_after_completion_is_rejected(live, participants, db, events):
    sid, key, _session = live
    row_id = participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")
    participants.append_participant_delta(sid, "plugin-relay", "pt", "settled")
    participants.complete_participant_message(sid, "plugin-relay", "pt")

    with pytest.raises(participants.UnknownTurnError):
        participants.append_participant_delta(sid, "plugin-relay", "pt", "too late")

    assert [r for r in db.get_messages(key) if r["id"] == row_id][0]["content"] == "settled"
    assert [d["text"] for d in _payloads(events, "participant.message.delta")] == ["settled"]


def test_a_delta_racing_completion_is_never_emitted_after_the_final_row(
    live, participants, db, events, monkeypatch
):
    sid, key, _session = live
    row_id = participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")
    participants.append_participant_delta(sid, "plugin-relay", "pt", "settled")

    original = db.update_message_row
    outcome: dict[str, Any] = {}

    def _racing_update(*args, **kwargs):
        # Completion has snapshotted the buffer and closed the turn but has not
        # written the row yet — the exact window a late delta could slip into.
        def _late():
            try:
                participants.append_participant_delta(sid, "plugin-relay", "pt", "racing")
                outcome["error"] = None
            except participants.ParticipantSeamError as exc:
                outcome["error"] = type(exc)

        thread = threading.Thread(target=_late)
        thread.start()
        thread.join(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "update_message_row", _racing_update)
    participants.complete_participant_message(sid, "plugin-relay", "pt")

    assert outcome["error"] is participants.UnknownTurnError
    assert [r for r in db.get_messages(key) if r["id"] == row_id][0]["content"] == "settled"
    assert [d["text"] for d in _payloads(events, "participant.message.delta")] == ["settled"]


def test_a_vanished_row_fails_the_finalize_instead_of_reporting_success(
    live, participants, db, events
):
    """A rewind (or a session-key rotation) between begin and complete.

    The update then matches no row. Reporting success would tell the caller —
    and any routing built on it — that a reply is in the transcript when the
    transcript has nothing.
    """
    sid, key, _session = live
    participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")
    participants.append_participant_delta(sid, "plugin-relay", "pt", "an answer")

    db.clear_messages(key)

    with pytest.raises(participants.ParticipantSeamError):
        participants.complete_participant_message(sid, "plugin-relay", "pt")

    assert _payloads(events, "participant.message.complete") == []
    assert db.get_messages(key) == []


def test_a_failed_finalize_reopens_the_turn(live, participants, db, monkeypatch):
    sid, key, _session = live
    row_id = participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")
    participants.append_participant_delta(sid, "plugin-relay", "pt", "one")

    def _boom(*args, **kwargs):
        raise RuntimeError("disk gremlin")

    original = db.update_message_row
    monkeypatch.setattr(db, "update_message_row", _boom)
    with pytest.raises(participants.ParticipantSeamError):
        participants.complete_participant_message(sid, "plugin-relay", "pt")

    monkeypatch.setattr(db, "update_message_row", original)
    participants.append_participant_delta(sid, "plugin-relay", "pt", " two")
    participants.complete_participant_message(sid, "plugin-relay", "pt")
    assert [r for r in db.get_messages(key) if r["id"] == row_id][0]["content"] == "one two"


def test_bad_status_is_rejected_and_leaves_the_turn_open(live, participants, db):
    sid, key, _session = live
    row_id = participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")
    with pytest.raises(participants.ParticipantSeamError):
        participants.complete_participant_message(sid, "plugin-relay", "pt", status="done")
    assert [r for r in db.get_messages(key) if r["id"] == row_id][0]["display_metadata"][
        "status"
    ] == "streaming"
    participants.complete_participant_message(sid, "plugin-relay", "pt", status="interrupted")


# ── ownership ───────────────────────────────────────────────────────────────


def test_publishing_requires_owning_the_participant(live, participants):
    sid, _key, _session = live
    with pytest.raises(participants.OwnershipError):
        participants.begin_participant_message(sid, "other-plugin", "claude:default", "pt")
    with pytest.raises(participants.OwnershipError):
        participants.begin_participant_message(sid, "plugin-relay", "codex:default", "pt")


def test_publishing_requires_owning_the_turn(live, participants):
    sid, _key, _session = live
    participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")
    with pytest.raises(participants.OwnershipError):
        participants.append_participant_delta(sid, "other-plugin", "pt", "x")
    with pytest.raises(participants.OwnershipError):
        participants.complete_participant_message(sid, "other-plugin", "pt")


def test_unknown_turn_is_typed(live, participants):
    sid, _key, _session = live
    with pytest.raises(participants.UnknownTurnError):
        participants.append_participant_delta(sid, "plugin-relay", "ghost", "x")
    with pytest.raises(participants.UnknownTurnError):
        participants.complete_participant_message(sid, "plugin-relay", "ghost")


# ── session resolution ──────────────────────────────────────────────────────


def test_resolver_prefers_a_valid_explicit_id(live, participants):
    sid, _key, _session = live
    assert participants.resolve_publish_session_id(sid) == sid


@pytest.mark.parametrize("explicit", ["not-a-session", "", "   "])
def test_resolver_rejects_an_unusable_explicit_id(live, participants, explicit):
    with pytest.raises(participants.UnknownSessionError):
        participants.resolve_publish_session_id(explicit)


def test_resolver_uses_the_live_ui_session_of_the_calling_context(
    live, participants, monkeypatch
):
    sid, _key, _session = live
    monkeypatch.setenv("HERMES_UI_SESSION_ID", sid)
    assert participants.resolve_publish_session_id() == sid


def test_resolver_never_falls_back_to_the_durable_session_id(live, participants, monkeypatch):
    _sid, key, _session = live
    monkeypatch.delenv("HERMES_UI_SESSION_ID", raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", key)
    with pytest.raises(participants.UnknownSessionError):
        participants.resolve_publish_session_id()


def test_resolver_fails_when_nothing_identifies_a_session(live, participants, monkeypatch):
    monkeypatch.delenv("HERMES_UI_SESSION_ID", raising=False)
    with pytest.raises(participants.UnknownSessionError):
        participants.resolve_publish_session_id()


def test_two_live_sessions_cannot_cross_route(server, participants, db, events):
    session_a = _make_session(server, db, "gw-a", "durable-a")
    session_b = _make_session(server, db, "gw-b", "durable-b")
    participants.register_participants("gw-a", "plugin-relay", [CLAUDE])
    participants.register_participants("gw-b", "plugin-relay", [CLAUDE])

    row_id = participants.begin_participant_message(
        "gw-a", "plugin-relay", "claude:default", "pt-a"
    )
    participants.append_participant_delta("gw-a", "plugin-relay", "pt-a", "for A only")
    participants.complete_participant_message("gw-a", "plugin-relay", "pt-a")

    assert len(session_a["history"]) == 1
    assert session_b["history"] == []
    assert [r["id"] for r in db.get_messages("durable-a")] == [row_id]
    assert db.get_messages("durable-b") == []

    participant_frames = [
        frame
        for frame in events
        if str(frame.get("params", {}).get("type", "")).startswith("participant.")
    ]
    assert participant_frames
    assert {frame["params"]["session_id"] for frame in participant_frames} == {"gw-a"}

    # B's own turn stays in B.
    participants.begin_participant_message("gw-b", "plugin-relay", "claude:default", "pt-b")
    with pytest.raises(participants.UnknownTurnError):
        participants.append_participant_delta("gw-a", "plugin-relay", "pt-b", "leak")


# ── interaction with shared transcript helpers ──────────────────────────────


def test_neither_kind_counts_as_a_user_turn(live, participants):
    from agent.context_compressor import is_user_originated_turn

    sid, _key, session = live
    participants.append_participant_user_message(sid, "plugin-relay", "@claude hi", ["claude"])
    participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")
    participants.complete_participant_message(sid, "plugin-relay", "pt", text="a reply")

    assert [is_user_originated_turn(entry) for entry in session["history"]] == [False, False]


def test_neither_kind_is_an_undo_or_rewind_target(live, participants, db):
    sid, key, _session = live
    db.append_message(key, "user", "a real ask")
    participants.append_participant_user_message(sid, "plugin-relay", "@claude hi", ["claude"])
    participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")
    participants.complete_participant_message(sid, "plugin-relay", "pt", text="a reply")

    targets = db.list_recent_user_messages(key)
    assert [t["preview"] for t in targets] == ["a real ask"]


def test_published_rows_are_marked_durable_for_the_transcript_flush(live, participants):
    from agent.context_compressor import _DB_PERSISTED_MARKER

    sid, _key, session = live
    participants.append_participant_user_message(sid, "plugin-relay", "@claude hi", ["claude"])
    participants.begin_participant_message(sid, "plugin-relay", "claude:default", "pt")

    assert all(entry.get(_DB_PERSISTED_MARKER) for entry in session["history"])
