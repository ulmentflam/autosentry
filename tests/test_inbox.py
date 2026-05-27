"""Monitor-side inbox consumer tests."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

from autosentry.config import load_config
from autosentry.inbox import InboxRecord, apply_commands, read_new

# ----- reading -------------------------------------------------------------


def _write_inbox(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_read_new_returns_all_when_no_cursor(tmp_path: Path):
    inbox = tmp_path / "slack_inbox.jsonl"
    _write_inbox(
        inbox,
        [
            {"id": "a", "user": "U1", "text": "abort", "command": "abort", "args": []},
            {"id": "b", "user": "U1", "text": "pause", "command": "pause", "args": []},
        ],
    )
    out = read_new(inbox, after_id=None)
    assert [r.id for r in out] == ["a", "b"]


def test_read_new_respects_cursor(tmp_path: Path):
    inbox = tmp_path / "slack_inbox.jsonl"
    _write_inbox(
        inbox,
        [
            {"id": "a", "user": "U1", "text": "abort", "command": "abort", "args": []},
            {"id": "b", "user": "U1", "text": "pause", "command": "pause", "args": []},
            {"id": "c", "user": "U1", "text": "resume", "command": "resume", "args": []},
        ],
    )
    out = read_new(inbox, after_id="a")
    assert [r.id for r in out] == ["b", "c"]


def test_read_new_returns_empty_for_missing_file(tmp_path: Path):
    assert read_new(tmp_path / "nope.jsonl", after_id=None) == []


def test_read_new_skips_malformed_lines(tmp_path: Path):
    inbox = tmp_path / "slack_inbox.jsonl"
    inbox.write_text(
        json.dumps({"id": "ok", "user": "U", "text": "abort", "command": "abort", "args": []})
        + "\nthis is not json\n"
    )
    out = read_new(inbox, after_id=None)
    assert [r.id for r in out] == ["ok"]


# ----- command application -------------------------------------------------


def _make_monitor_stub(tmp_path: Path):
    """Build a Monitor against a minimal config, but stub the supervisor."""
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            """\
            process:
              kind: local
              command: ["true"]
              restart_policy:
                max_restarts: 3
            monitor:
              log_dir: ".autosentry/logs"
              poll_interval_seconds: 1
            detectors: []
            rules: []
            healing:
              claude:
                enabled: false
            state_path: ".autosentry/state.json"
            incidents_dir: ".autosentry/incidents"
            """
        )
    )
    cfg = load_config(cfg_path)

    # Avoid actually starting a process or installing signal handlers.
    from autosentry.monitor import Monitor

    monitor = Monitor(cfg)
    monitor.supervisor = MagicMock()
    return monitor


def test_apply_commands_handles_abort(tmp_path: Path):
    monitor = _make_monitor_stub(tmp_path)
    inbox = tmp_path / "slack_inbox.jsonl"
    _write_inbox(
        inbox,
        [{"id": "1", "user": "U1", "text": "abort", "command": "abort", "args": []}],
    )
    applied = apply_commands(monitor, inbox)
    assert applied == 1
    assert monitor._stop is True
    monitor.supervisor.stop.assert_called_once()
    assert monitor.state.user["last_processed_inbox_id"] == "1"


def test_apply_commands_handles_pause_and_resume(tmp_path: Path):
    monitor = _make_monitor_stub(tmp_path)
    inbox = tmp_path / "slack_inbox.jsonl"
    _write_inbox(
        inbox,
        [
            {"id": "1", "user": "U1", "text": "pause", "command": "pause", "args": []},
            {"id": "2", "user": "U1", "text": "resume", "command": "resume", "args": []},
        ],
    )
    applied = apply_commands(monitor, inbox)
    assert applied == 2
    monitor.supervisor.stop.assert_called_once()
    monitor.supervisor.start.assert_called_once()


def test_apply_commands_handles_set_max_restarts(tmp_path: Path):
    monitor = _make_monitor_stub(tmp_path)
    inbox = tmp_path / "slack_inbox.jsonl"
    _write_inbox(
        inbox,
        [
            {
                "id": "1",
                "user": "U1",
                "text": "set max_restarts 10",
                "command": "set",
                "args": ["max_restarts", "10"],
            }
        ],
    )
    applied = apply_commands(monitor, inbox)
    assert applied == 1
    assert monitor.state.max_restarts == 10


def test_apply_commands_handles_invalid_set_value(tmp_path: Path):
    monitor = _make_monitor_stub(tmp_path)
    original = monitor.state.max_restarts
    inbox = tmp_path / "slack_inbox.jsonl"
    _write_inbox(
        inbox,
        [
            {
                "id": "1",
                "user": "U1",
                "text": "set max_restarts boom",
                "command": "set",
                "args": ["max_restarts", "boom"],
            }
        ],
    )
    apply_commands(monitor, inbox)
    # State unchanged because the value didn't parse.
    assert monitor.state.max_restarts == original


def test_apply_commands_records_comment(tmp_path: Path):
    monitor = _make_monitor_stub(tmp_path)
    inbox = tmp_path / "slack_inbox.jsonl"
    _write_inbox(
        inbox,
        [
            {
                "id": "1",
                "user": "U1",
                "text": "comment: looks fine",
                "command": "comment",
                "args": ["looks fine"],
            }
        ],
    )
    apply_commands(monitor, inbox)
    assert monitor.state.user["comments"][0]["text"] == "looks fine"


def test_apply_commands_skips_already_processed(tmp_path: Path):
    monitor = _make_monitor_stub(tmp_path)
    inbox = tmp_path / "slack_inbox.jsonl"
    _write_inbox(
        inbox,
        [
            {"id": "1", "user": "U1", "text": "abort", "command": "abort", "args": []},
            {"id": "2", "user": "U1", "text": "pause", "command": "pause", "args": []},
        ],
    )
    # Pre-populate cursor as if "1" was already applied.
    monitor.state.user["last_processed_inbox_id"] = "1"
    applied = apply_commands(monitor, inbox)
    assert applied == 1
    # Only pause should have fired, not abort.
    assert monitor._stop is False
    monitor.supervisor.stop.assert_called_once()


def test_inbox_record_dataclass_carries_args():
    rec = InboxRecord(id="x", user="U", text="set k v", command="set", args=["k", "v"])
    assert rec.args == ["k", "v"]
