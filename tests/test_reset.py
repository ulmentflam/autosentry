"""Tests for `autosentry reset` and the underlying state helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from autosentry.cli import app
from autosentry.state import (
    MonitorState,
    ResetRecord,
    RestartRecord,
    StateStore,
    append_reset_log,
)

runner = CliRunner()


# ----- state-level helpers -------------------------------------------------


def test_record_reset_clears_counter_keeps_total_and_history():
    state = MonitorState(restarts=7, restarts_total=42)
    state.record_restart("flaky test")  # +1 to both counters → 8 / 43
    record = state.record_reset(reason="cleared mid-debug")

    assert state.restarts == 0  # the budget counter is zeroed
    assert state.restarts_total == 43  # audit total is preserved
    assert len(state.restart_history) == 1  # restart_history preserved
    assert len(state.reset_history) == 1
    assert record.prior_restarts == 8
    assert record.prior_restarts_total == 43
    assert record.cleared_history is False
    assert record.source == "manual"


def test_record_reset_full_drops_restart_history():
    state = MonitorState(restarts=3)
    state.restart_history.append(RestartRecord(time="t", reason="r"))
    state.record_reset(reason="full wipe", clear_history=True)
    assert state.restart_history == []
    assert state.reset_history[-1].cleared_history is True


def test_record_reset_caps_history_at_200():
    state = MonitorState()
    for i in range(205):
        state.record_reset(reason=f"reset-{i}")
    assert len(state.reset_history) == 200
    # We kept the most recent — the first one rolled off.
    assert state.reset_history[-1].reason == "reset-204"


def test_append_reset_log_writes_one_line_per_event(tmp_path: Path):
    log_path = tmp_path / "logs" / "reset.log"
    rec = ResetRecord(
        time="2026-06-04T12:00:00+00:00",
        reason="parked at max_restarts",
        prior_restarts=10,
        prior_restarts_total=42,
        source="manual",
    )
    append_reset_log(log_path, rec)
    append_reset_log(
        log_path,
        ResetRecord(
            time="2026-06-04T12:05:00+00:00",
            reason="pipeline advance: pretrain → sft",
            prior_restarts=2,
            prior_restarts_total=44,
            stage="pretrain",
            source="pipeline",
        ),
    )
    lines = log_path.read_text().splitlines()
    assert len(lines) == 2
    assert "source=manual" in lines[0]
    assert "prior_restarts=10" in lines[0]
    assert "source=pipeline" in lines[1]
    assert "stage=pretrain" in lines[1]


def test_state_round_trips_reset_history(tmp_path: Path):
    """ResetRecord must survive a state.json round-trip — that's what
    makes `autosentry status` able to show prior resets after a restart."""
    store = StateStore(tmp_path / "state.json")
    state = MonitorState(restarts=4)
    state.record_reset(reason="manual reset before merge")
    store.save(state)

    reloaded = store.load()
    assert reloaded.restarts == 0
    assert len(reloaded.reset_history) == 1
    assert reloaded.reset_history[0].reason == "manual reset before merge"


# ----- CLI behavior --------------------------------------------------------


@pytest.fixture
def reset_repo(tmp_path: Path, minimal_config) -> tuple[Path, Path]:  # noqa: ANN001
    """A repo with a state.json carrying a non-zero restart counter.

    Returns (config_path, state_path) so tests can drive `autosentry reset`
    against the same store the CLI will look at.
    """
    state_path = tmp_path / ".autosentry" / "state.json"
    store = StateStore(state_path)
    state = MonitorState(restarts=5, restarts_total=12, pid=None)
    state.restart_history.append(RestartRecord(time="t1", reason="prior"))
    store.save(state)
    return tmp_path / "autosentry.yaml", state_path


def test_reset_clears_counter_and_writes_log(tmp_path: Path, reset_repo):
    config_path, state_path = reset_repo
    result = runner.invoke(
        app,
        [
            "reset",
            "--config",
            str(config_path),
            "--reason",
            "test reset",
            "--force",
            "--no-restart",
        ],
    )
    assert result.exit_code == 0, result.stdout
    reloaded = StateStore(state_path).load()
    assert reloaded.restarts == 0
    assert reloaded.restarts_total == 12  # preserved
    assert len(reloaded.restart_history) == 1  # preserved by default
    assert reloaded.reset_history[-1].prior_restarts == 5
    assert reloaded.reset_history[-1].reason == "test reset"
    # reset.log mirror exists and contains the structured fields.
    reset_log = tmp_path / ".autosentry" / "logs" / "reset.log"
    assert reset_log.exists()
    body = reset_log.read_text()
    assert "source=manual" in body
    assert "prior_restarts=5" in body
    assert "test reset" in body


def test_reset_full_requires_force(tmp_path: Path, reset_repo):
    config_path, _ = reset_repo
    result = runner.invoke(
        app,
        ["reset", "--config", str(config_path), "--full", "--no-restart"],
    )
    assert result.exit_code == 2
    assert "--force" in result.stdout


def test_reset_full_with_force_drops_history(tmp_path: Path, reset_repo):
    config_path, state_path = reset_repo
    result = runner.invoke(
        app,
        [
            "reset",
            "--config",
            str(config_path),
            "--full",
            "--force",
            "--no-restart",
        ],
    )
    assert result.exit_code == 0, result.stdout
    reloaded = StateStore(state_path).load()
    assert reloaded.restart_history == []
    assert reloaded.reset_history[-1].cleared_history is True


def test_reset_with_no_state_exits_nonzero(tmp_path: Path, minimal_config):  # noqa: ANN001
    # Point at a config whose state path doesn't exist yet.
    config_path = tmp_path / "autosentry.yaml"
    result = runner.invoke(app, ["reset", "--config", str(config_path), "--force", "--no-restart"])
    assert result.exit_code != 0
    assert "nothing to reset" in result.stdout
