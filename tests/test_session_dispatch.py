"""Tests for the session-dispatch flow (autosentry 0.9.0).

Covers the four phases of the change in tight isolation:

- Phase 1: ``dispatch.mode: session`` makes the monitor skip the
  rule/Claude healer and touch the dispatch marker instead.
- Phase 2: ``autosentry probe`` reports monitor liveness + pending
  incidents + rule matches, and emits a Stop-hook payload only when
  there's work.
- Phase 3: the Stop-hook installer is idempotent and survives a
  pre-existing settings file.
- Phase 4 (action queue): the monitor drains the queue in session
  mode and applies actions via the supervisor.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from autosentry.config import load_config
from autosentry.detectors.base import Detection
from autosentry.hooks import (
    STOP_HOOK_COMMAND,
    install_claude_stop_hook,
    uninstall_claude_stop_hook,
)
from autosentry.monitor import Monitor


def _write_cfg(tmp_path: Path, *, dispatch_mode: str | None = None) -> Path:
    cfg = tmp_path / "autosentry.yaml"
    body = dedent(
        """\
        process:
          kind: local
          command: ["true"]
          restart_policy:
            max_restarts: 2
            cooldown_seconds: 0
        monitor:
          poll_interval_seconds: 1
          stall_timeout_seconds: 60
        detectors:
          - kind: pattern
            name: oom
            regex: "OutOfMemoryError"
          - kind: traceback
        rules:
          - name: oom-restart
            match: { detector: oom }
            action: { kind: restart, notify: false }
        healing:
          claude:
            enabled: false
        state_path: ".autosentry/state.json"
        incidents_dir: ".autosentry/incidents"
        """
    )
    if dispatch_mode:
        body += f"dispatch:\n  mode: {dispatch_mode}\n"
    cfg.write_text(body)
    return cfg


def _monitor(tmp_path: Path, monkeypatch, *, dispatch_mode: str | None = None) -> Monitor:
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path, dispatch_mode=dispatch_mode))
    m = Monitor(cfg)
    m.supervisor = MagicMock()
    m.supervisor.status.return_value = MagicMock(running=True, pid=12345, exit_code=None)
    m._notify = MagicMock()
    return m


# ----- Phase 1: detector-only dispatch -------------------------------------


def test_session_mode_skips_rule_healer_and_touches_marker(tmp_path: Path, monkeypatch) -> None:
    """Under ``dispatch.mode: session`` the monitor must NOT run the rule
    healer (even when a rule would match) and must touch the dispatch
    marker so the session knows to wake up."""
    m = _monitor(tmp_path, monkeypatch, dispatch_mode="session")
    m.rule_healer.attempt = MagicMock()  # type: ignore[method-assign]

    det = Detection(
        kind="error",
        detector="oom",
        message="OutOfMemoryError: CUDA",
        trace="",
        log_context=[],
        meta={},
    )
    m._fire_detection(det)

    m.rule_healer.attempt.assert_not_called()
    marker = m.cfg.resolve(m.cfg.dispatch.request_marker)
    assert marker.exists(), "session-dispatch marker should be touched"


def test_builtin_mode_still_runs_rule_healer(tmp_path: Path, monkeypatch) -> None:
    """Sanity check the default (``builtin``) path is unchanged."""
    m = _monitor(tmp_path, monkeypatch)
    assert m.cfg.dispatch.mode == "builtin"
    # Existing rule healer should be invoked for an oom detection.
    m.rule_healer.attempt = MagicMock(return_value=None)  # type: ignore[method-assign]
    det = Detection(
        kind="error",
        detector="oom",
        message="OutOfMemoryError: CUDA",
        trace="",
        log_context=[],
        meta={},
    )
    m._fire_detection(det)
    m.rule_healer.attempt.assert_called_once()


# ----- Phase 2: probe command ----------------------------------------------


def test_probe_reports_monitor_down_and_pending(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture
) -> None:
    """End-to-end: write a state.json with a definitely-dead pid + a
    pending incident in index.jsonl, then run probe and check the JSON."""
    monkeypatch.chdir(tmp_path)
    cfg_path = _write_cfg(tmp_path, dispatch_mode="session")
    state_dir = tmp_path / ".autosentry"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "state.json").write_text(
        json.dumps({"pid": 1, "last_heartbeat": "2020-01-01T00:00:00+00:00"})
    )
    incidents_dir = state_dir / "incidents"
    incidents_dir.mkdir(exist_ok=True)
    (incidents_dir / "index.jsonl").write_text(
        json.dumps(
            {
                "id": "2026-05-30T18-14-22Z-error-oom",
                "kind": "error",
                "detector": "oom",
                "message": "OutOfMemoryError: CUDA",
                "rule": None,
                "action": None,
            }
        )
        + "\n"
    )

    from autosentry.cli.commands.probe import _build_payload

    cfg = load_config(cfg_path)
    payload = _build_payload(cfg)

    assert payload["dispatch_mode"] == "session"
    assert payload["monitor"]["pid_alive"] is False, "pid=1 should not be reported alive"
    assert payload["monitor"]["stale"] is True, "ancient heartbeat should be stale"
    assert len(payload["pending_incidents"]) == 1
    inc = payload["pending_incidents"][0]
    assert inc["detector"] == "oom"
    assert inc["rule_match"] == "oom-restart"
    assert inc["suggested_action"]["kind"] == "restart"


def test_probe_inject_prompt_returns_none_when_idle(tmp_path: Path, monkeypatch) -> None:
    """No pending work + healthy monitor → no Stop-hook payload (the hook
    lets the turn conclude)."""
    from autosentry.cli.commands.probe import _inject_prompt

    payload = {
        "monitor": {"pid_alive": True, "stale": False, "pid": 42, "heartbeat_age_seconds": 1.0},
        "pending_incidents": [],
    }
    assert _inject_prompt(payload) is None


def test_probe_inject_prompt_emits_block_when_monitor_down(tmp_path: Path) -> None:
    from autosentry.cli.commands.probe import _inject_prompt

    payload = {
        "monitor": {
            "pid_alive": False,
            "stale": False,
            "pid": 99,
            "heartbeat_age_seconds": None,
        },
        "pending_incidents": [],
    }
    out = _inject_prompt(payload)
    assert out is not None
    assert out["decision"] == "block"
    assert "Monitor is DOWN" in out["reason"]


def test_probe_advance_cursor_filters_subsequent_entries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_path = _write_cfg(tmp_path, dispatch_mode="session")
    (tmp_path / ".autosentry").mkdir(exist_ok=True)
    (tmp_path / ".autosentry" / "incidents").mkdir(exist_ok=True)
    (tmp_path / ".autosentry" / "incidents" / "index.jsonl").write_text(
        "\n".join(
            json.dumps({"id": f"id-{i}", "detector": "oom", "message": "x"}) for i in range(3)
        )
        + "\n"
    )

    from autosentry.cli.commands.probe import _build_payload, _write_cursor

    cfg = load_config(cfg_path)
    assert len(_build_payload(cfg)["pending_incidents"]) == 3

    _write_cursor(cfg, "id-1")
    assert len(_build_payload(cfg)["pending_incidents"]) == 1


# ----- Phase 3: Stop-hook installer ----------------------------------------


def test_install_claude_stop_hook_creates_file(tmp_path: Path) -> None:
    result = install_claude_stop_hook(tmp_path)
    assert result.status == "created"
    settings = json.loads(result.settings_path.read_text())
    assert any(
        h["command"] == STOP_HOOK_COMMAND
        for group in settings["hooks"]["Stop"]
        for h in group["hooks"]
    )


def test_install_claude_stop_hook_is_idempotent(tmp_path: Path) -> None:
    install_claude_stop_hook(tmp_path)
    second = install_claude_stop_hook(tmp_path)
    assert second.status == "already_present"
    settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    # Exactly one Stop hook group with our command.
    matches = [
        h
        for group in settings["hooks"]["Stop"]
        for h in group["hooks"]
        if h["command"] == STOP_HOOK_COMMAND
    ]
    assert len(matches) == 1


def test_install_claude_stop_hook_preserves_existing(tmp_path: Path) -> None:
    """If the user already has hooks configured, we add to them — not
    overwrite. The other hooks survive."""
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo existing"}]}]}}
        )
    )
    result = install_claude_stop_hook(tmp_path)
    assert result.status == "added"
    settings = json.loads(settings_path.read_text())
    commands = [h["command"] for group in settings["hooks"]["Stop"] for h in group["hooks"]]
    assert "echo existing" in commands
    assert STOP_HOOK_COMMAND in commands


def test_uninstall_claude_stop_hook(tmp_path: Path) -> None:
    install_claude_stop_hook(tmp_path)
    assert uninstall_claude_stop_hook(tmp_path) is True
    # Second uninstall is a no-op.
    assert uninstall_claude_stop_hook(tmp_path) is False


# ----- Phase 4: action queue -----------------------------------------------


def test_drain_session_action_queue_applies_actions(tmp_path: Path, monkeypatch) -> None:
    """In session mode, the monitor's tick should drain the action queue
    and call ``supervisor.apply_action`` for each new entry."""
    m = _monitor(tmp_path, monkeypatch, dispatch_mode="session")
    m.supervisor.apply_action = MagicMock()  # type: ignore[method-assign]

    queue_path = m.cfg.resolve(m.cfg.dispatch.action_queue_path)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "a-001",
                        "incident_id": "inc-1",
                        "rule": "oom-restart",
                        "source": "rule",
                        "action": {
                            "kind": "restart",
                            "set": {},
                            "command": [],
                            "notify": False,
                        },
                    }
                ),
                json.dumps(
                    {
                        "id": "a-002",
                        "incident_id": "inc-2",
                        "rule": None,
                        "source": "claude",
                        "action": {
                            "kind": "restart_with_env",
                            "set": {"BATCH_SIZE": "4"},
                            "command": [],
                            "notify": False,
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    m._drain_session_action_queue()

    assert m.supervisor.apply_action.call_count == 2
    cursor = m.cfg.resolve(m.cfg.dispatch.action_cursor_path).read_text().strip()
    assert cursor == "a-002"


def test_drain_session_action_queue_skips_already_applied(tmp_path: Path, monkeypatch) -> None:
    m = _monitor(tmp_path, monkeypatch, dispatch_mode="session")
    m.supervisor.apply_action = MagicMock()  # type: ignore[method-assign]

    queue_path = m.cfg.resolve(m.cfg.dispatch.action_queue_path)
    cursor_path = m.cfg.resolve(m.cfg.dispatch.action_cursor_path)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(
        json.dumps(
            {
                "id": "a-001",
                "incident_id": "inc-1",
                "rule": None,
                "source": "manual",
                "action": {"kind": "restart", "set": {}, "command": [], "notify": False},
            }
        )
        + "\n"
    )
    cursor_path.write_text("a-001")  # already applied

    m._drain_session_action_queue()
    m.supervisor.apply_action.assert_not_called()


def test_drain_session_action_queue_logs_and_continues_on_bad_action(
    tmp_path: Path, monkeypatch
) -> None:
    """A malformed action shouldn't take the monitor down — log it,
    advance the cursor, keep going."""
    m = _monitor(tmp_path, monkeypatch, dispatch_mode="session")
    m.supervisor.apply_action = MagicMock()  # type: ignore[method-assign]

    queue_path = m.cfg.resolve(m.cfg.dispatch.action_queue_path)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "a-001",
                        "incident_id": "inc-1",
                        "source": "rule",
                        "action": {"kind": "no_such_kind"},  # invalid
                    }
                ),
                json.dumps(
                    {
                        "id": "a-002",
                        "incident_id": "inc-2",
                        "source": "rule",
                        "action": {"kind": "restart", "set": {}, "command": [], "notify": False},
                    }
                ),
            ]
        )
        + "\n"
    )

    m._drain_session_action_queue()

    # Only the valid action was applied; cursor advanced past both.
    m.supervisor.apply_action.assert_called_once()
    cursor = m.cfg.resolve(m.cfg.dispatch.action_cursor_path).read_text().strip()
    assert cursor == "a-002"
