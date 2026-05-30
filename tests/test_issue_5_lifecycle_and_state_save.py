"""Regression tests for issue #5.

Two distinct bugs, same incident:

- Bug 1: monitor never exited after a supervised child cleanly exited 0
  (would sit idle indefinitely).
- Bug 2: ``StateStore.save`` raised ``FileNotFoundError`` when a sibling
  process removed the ``.tmp`` file between write and rename (observed
  on iCloud-synced directories), and the monitor logged it every tick
  with no backoff — ~16M ``state save failed`` lines in 24 minutes.
"""

from __future__ import annotations

import time
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

from autosentry.config import load_config
from autosentry.monitor import Monitor
from autosentry.state import StateStore


def _write_cfg(tmp_path: Path, lifecycle: str | None = None) -> Path:
    body = dedent(
        """\
        process:
          kind: local
          command: ["true"]
          restart_policy:
            max_restarts: 4
            cooldown_seconds: 0
        healing:
          claude:
            enabled: false
        state_path: ".autosentry/state.json"
        incidents_dir: ".autosentry/incidents"
        """
    )
    if lifecycle:
        body = body.replace(
            'command: ["true"]\n',
            f'command: ["true"]\n  lifecycle: {lifecycle}\n',
        )
    cfg = tmp_path / "autosentry.yaml"
    cfg.write_text(body)
    return cfg


def _monitor(tmp_path: Path, monkeypatch, lifecycle: str | None = None) -> Monitor:
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path, lifecycle=lifecycle))
    monitor = Monitor(cfg)
    monitor.supervisor = MagicMock()
    monitor.supervisor.status.return_value = MagicMock(running=False, pid=None, exit_code=0)
    monitor._notify = MagicMock()
    return monitor


# ----- Bug 1: lifecycle gates monitor exit on child exit ------------------


def test_handle_exit_default_restart_on_failure_stops_on_clean_exit(
    tmp_path: Path, monkeypatch
) -> None:
    """Default ``restart_on_failure`` lifecycle: clean exit ends the monitor."""
    monitor = _monitor(tmp_path, monkeypatch)
    assert monitor.cfg.process.lifecycle == "restart_on_failure"
    cont = monitor._handle_exit(0)
    assert cont is False
    assert monitor._final_exit_code == 0


def test_handle_exit_one_shot_stops_on_any_exit(tmp_path: Path, monkeypatch) -> None:
    """``one_shot``: any exit code ends the monitor, no healing attempted."""
    monitor = _monitor(tmp_path, monkeypatch, lifecycle="one_shot")
    assert monitor._handle_exit(0) is False
    assert monitor._final_exit_code == 0
    # Non-zero exit also ends the supervisor under one_shot.
    monitor._final_exit_code = None
    assert monitor._handle_exit(2) is False
    assert monitor._final_exit_code == 2


def test_handle_exit_restart_always_keeps_running_on_clean_exit(
    tmp_path: Path, monkeypatch
) -> None:
    """``restart_always``: clean exit still routes through the restart path
    (the pre-0.8.5 behavior, available for users who genuinely want it).
    """
    monitor = _monitor(tmp_path, monkeypatch, lifecycle="restart_always")
    cont = monitor._handle_exit(0)
    assert cont is True
    assert monitor._final_exit_code == 0


def test_handle_exit_restart_on_failure_still_routes_nonzero(tmp_path: Path, monkeypatch) -> None:
    """Non-zero exit still goes through the standard restart path under
    the default ``restart_on_failure`` lifecycle."""
    monitor = _monitor(tmp_path, monkeypatch)
    cont = monitor._handle_exit(2)
    assert cont is True


# ----- Bug 2: StateStore + monitor backoff ---------------------------------


def test_state_store_falls_back_to_direct_write_when_tmp_vanishes(
    tmp_path: Path, monkeypatch
) -> None:
    """If ``os.replace`` raises ``FileNotFoundError`` (the iCloud evictor
    case from issue #5), StateStore should still produce a state file
    rather than propagating the error forever."""
    import os as os_mod

    store = StateStore(tmp_path / "state.json")
    state = store.load()
    state.pid = 12345

    real_replace = os_mod.replace
    call_count = {"n": 0}

    def flaky_replace(src, dst):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise FileNotFoundError(2, "No such file or directory", str(src))
        return real_replace(src, dst)

    monkeypatch.setattr("autosentry.state.os.replace", flaky_replace)
    # Should not raise. Direct-write fallback writes the payload to
    # state.json itself.
    store.save(state)
    assert (tmp_path / "state.json").exists()
    reloaded = store.load()
    assert reloaded.pid == 12345


def test_monitor_save_state_backs_off_on_repeated_failures(tmp_path: Path, monkeypatch) -> None:
    """A flapping state-save must not produce one error log per tick.

    Simulate ``state_store.save`` failing every time and verify:
      - the first failure logs once,
      - subsequent calls inside the backoff window are *not* re-attempted
        (the suppressed counter advances), and
      - the public ``log().error`` is invoked at most a handful of times,
        nowhere near the 11K/sec rate seen in the bug report.
    """
    monitor = _monitor(tmp_path, monkeypatch)
    err = FileNotFoundError(2, "No such file or directory", "state.json.tmp")
    monitor.state_store = MagicMock()
    monitor.state_store.save.side_effect = err

    # Slam _save_state in a tight loop the way the run loop would.
    for _ in range(5000):
        monitor._save_state()

    # The store should have been called at most a small number of times —
    # one initial attempt, the rest are suppressed by the backoff window
    # (base 0.5s; 5000 iters happen in well under that).
    assert monitor.state_store.save.call_count <= 5, (
        f"expected ≤5 actual save attempts under backoff, got {monitor.state_store.save.call_count}"
    )
    assert monitor._state_save_fail_count >= 1
    assert monitor._state_save_suppressed >= 4990


def test_monitor_save_state_clears_backoff_on_success(tmp_path: Path, monkeypatch) -> None:
    """Once a save succeeds, the backoff bookkeeping resets so future
    failures emit a fresh error log instead of staying silent."""
    monitor = _monitor(tmp_path, monkeypatch)
    monitor.state_store = MagicMock()
    err = FileNotFoundError(2, "No such file or directory", "state.json.tmp")

    # First call fails — sets up backoff.
    monitor.state_store.save.side_effect = err
    monitor._save_state()
    assert monitor._state_save_fail_count == 1

    # Force the next attempt to be allowed, then have the store succeed.
    monitor._state_save_next_attempt = time.monotonic() - 1
    monitor.state_store.save.side_effect = None
    monitor.state_store.save.return_value = None
    monitor._save_state()
    assert monitor._state_save_fail_count == 0
    assert monitor._state_save_last_error is None
