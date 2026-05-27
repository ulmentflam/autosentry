"""Dispatcher tests — command parsing, outbox round-trip, inbound capture."""

from __future__ import annotations

import json
from pathlib import Path

from autosentry.dispatcher import (
    DispatcherDaemon,
    DispatcherState,
    InboxEntry,
    OutboxEntry,
    StdoutBackend,
    parse_command,
)
from autosentry.dispatcher.backends import Backend, DeliveryResult, ReplyMessage

# ----- parse_command -------------------------------------------------------


def test_parse_command_simple_verbs():
    for verb in ("abort", "pause", "resume", "approve"):
        cmd, args = parse_command(verb)
        assert (cmd, args) == (verb, [])


def test_parse_command_is_case_insensitive():
    cmd, args = parse_command("Abort")
    assert cmd == "abort"


def test_parse_command_set_extracts_key_and_value():
    cmd, args = parse_command("set max_restarts 10")
    assert cmd == "set"
    assert args == ["max_restarts", "10"]


def test_parse_command_comment_captures_text():
    cmd, args = parse_command("comment: this looks fishy")
    assert cmd == "comment"
    assert args == ["this looks fishy"]


def test_parse_command_falls_through_for_plain_prose():
    cmd, args = parse_command("not a command, just thoughts")
    assert (cmd, args) == (None, [])


def test_parse_command_empty_input():
    assert parse_command("") == (None, [])
    assert parse_command("   ") == (None, [])


# ----- outbox round-trip ---------------------------------------------------


def test_outbox_entry_from_minimal_dict():
    e = OutboxEntry.from_dict({"id": "x", "title": "T", "body": "B", "kind": "info"})
    assert e.id == "x"
    assert e.sent is False
    assert e.thread_ts is None


def test_dispatcher_tick_delivers_pending_and_marks_sent(tmp_path: Path):
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text(
        json.dumps(
            {
                "id": "msg-1",
                "channel": "C123",
                "thread_key": "pipeline",
                "kind": "anomaly",
                "title": "Stall",
                "body": "step 8450 has not advanced",
                "sent": False,
            }
        )
        + "\n"
    )

    daemon = DispatcherDaemon(
        backend=StdoutBackend(),
        outbox_path=outbox,
        inbox_path=tmp_path / "inbox.jsonl",
        state_path=tmp_path / "dispatcher_state.json",
    )
    daemon.tick_once()

    rewritten = [
        json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(rewritten) == 1
    assert rewritten[0]["sent"] is True
    assert rewritten[0]["delivered_at"] is not None


def test_dispatcher_state_round_trips(tmp_path: Path):
    state_path = tmp_path / "state.json"
    s = DispatcherState(
        threads={"pipeline": "1716738000.000100"},
        last_seen_reply={"pipeline": "1716738001.000200"},
        bot_user_id="UBOT",
    )
    s.save(state_path)
    loaded = DispatcherState.load(state_path)
    assert loaded.threads == s.threads
    assert loaded.last_seen_reply == s.last_seen_reply
    assert loaded.bot_user_id == "UBOT"


# ----- inbound polling -----------------------------------------------------


class _FakeBackend(Backend):
    """Backend stub for testing inbound polling end-to-end."""

    name = "fake"

    def __init__(self, replies: list[ReplyMessage]):
        self._replies = replies
        self.delivered: list[tuple[str | None, str, str | None]] = []

    def deliver(self, *, channel, text, thread_ts=None):
        self.delivered.append((channel, text, thread_ts))
        # Return a synthetic ts so the dispatcher pins it as the parent.
        return DeliveryResult(ok=True, ts="1716738000.000100")

    def fetch_replies(self, *, channel, thread_ts, after_ts):
        # Mimic the SlackApiBackend, which uses oldest=after_ts +
        # inclusive=false: a non-empty after_ts means callers want STRICTLY
        # newer messages. Without this, the dispatcher would re-ingest the
        # same replies on every tick and the test would (correctly) catch it.
        if not after_ts:
            return list(self._replies)
        return [r for r in self._replies if r.ts > after_ts]

    def supports_inbound(self) -> bool:
        return True


def test_dispatcher_inbound_appends_parsed_commands(tmp_path: Path):
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text(
        json.dumps(
            {
                "id": "msg-1",
                "channel": "C123",
                "thread_key": "pipeline",
                "kind": "anomaly",
                "title": "Stall",
                "body": "...",
                "sent": False,
            }
        )
        + "\n"
    )
    backend = _FakeBackend(
        replies=[
            ReplyMessage(
                ts="1716738010.000100",
                user="U999",
                text="set max_restarts 10",
                thread_ts="1716738000.000100",
            ),
            ReplyMessage(
                ts="1716738020.000100",
                user="U999",
                text="abort",
                thread_ts="1716738000.000100",
            ),
            ReplyMessage(
                ts="1716738030.000100",
                user="U999",
                text="just a thought",
                thread_ts="1716738000.000100",
            ),
        ]
    )
    inbox = tmp_path / "inbox.jsonl"
    daemon = DispatcherDaemon(
        backend=backend,
        outbox_path=outbox,
        inbox_path=inbox,
        state_path=tmp_path / "state.json",
        default_channel="C123",
    )
    # First tick: deliver the outbound, which establishes the thread parent.
    daemon.tick_once()
    # Second tick: now that the parent is recorded, inbound polling fires.
    daemon.tick_once()

    lines = [json.loads(line) for line in inbox.read_text().splitlines() if line.strip()]
    assert len(lines) == 3
    assert lines[0]["command"] == "set"
    assert lines[0]["args"] == ["max_restarts", "10"]
    assert lines[1]["command"] == "abort"
    assert lines[2]["command"] is None  # free-form prose


def test_dispatcher_skips_outbox_when_mtime_unchanged(tmp_path: Path):
    """Once the outbox is drained, a subsequent tick shouldn't reread it."""
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text(
        json.dumps(
            {
                "id": "msg-1",
                "channel": "C123",
                "thread_key": None,
                "kind": "info",
                "title": "T",
                "body": "B",
                "sent": False,
            }
        )
        + "\n"
    )
    backend = _FakeBackend(replies=[])
    daemon = DispatcherDaemon(
        backend=backend,
        outbox_path=outbox,
        inbox_path=tmp_path / "inbox.jsonl",
        state_path=tmp_path / "state.json",
        default_channel="C123",
        inbound_marker_path=tmp_path / "marker",
        idle_inbound_seconds=0,  # marker is the only inbound trigger
    )
    # First tick: delivers + records mtime.
    daemon.tick_once()
    assert len(backend.delivered) == 1

    # Second tick: outbox content is identical, mtime didn't move → no
    # additional delivery attempts.
    daemon.tick_once()
    assert len(backend.delivered) == 1


def test_dispatcher_polls_inbound_only_when_marker_advances(tmp_path: Path):
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text(
        json.dumps(
            {
                "id": "msg-1",
                "channel": "C123",
                "thread_key": "pipeline",
                "kind": "anomaly",
                "title": "T",
                "body": "B",
                "sent": False,
            }
        )
        + "\n"
    )
    marker = tmp_path / "marker"
    marker.touch()
    backend = _FakeBackend(
        replies=[
            ReplyMessage(
                ts="1.0",
                user="U1",
                text="abort",
                thread_ts="1716738000.000100",
            )
        ]
    )

    class _CountingBackend(_FakeBackend):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fetch_calls = 0

        def fetch_replies(self, **kwargs):
            self.fetch_calls += 1
            return super().fetch_replies(**kwargs)

    backend = _CountingBackend(
        replies=[
            ReplyMessage(
                ts="2.0",
                user="U1",
                text="pause",
                thread_ts="1716738000.000100",
            )
        ]
    )

    daemon = DispatcherDaemon(
        backend=backend,
        outbox_path=outbox,
        inbox_path=tmp_path / "inbox.jsonl",
        state_path=tmp_path / "state.json",
        default_channel="C123",
        inbound_marker_path=marker,
        idle_inbound_seconds=0,  # marker is the only inbound trigger
    )
    # First tick: drains outbox, sees marker is set (mtime advanced from 0),
    # polls Slack once.
    daemon.tick_once()
    first_calls = backend.fetch_calls
    assert first_calls == 1

    # Second tick: nothing has changed (outbox mtime same, marker mtime
    # same), so we should NOT call fetch_replies again.
    daemon.tick_once()
    assert backend.fetch_calls == first_calls

    # Touch the marker → next tick should re-poll.
    import time as _t

    _t.sleep(0.05)  # ensure st_mtime resolution can distinguish
    marker.touch()
    daemon.tick_once()
    assert backend.fetch_calls == first_calls + 1


def test_inbox_entry_defaults():
    e = InboxEntry(
        id="x",
        ts="1.0",
        thread_key="pipeline",
        channel="C",
        user="U",
        text="abort",
    )
    assert e.command is None
    assert e.args == []
    assert e.processed is False
