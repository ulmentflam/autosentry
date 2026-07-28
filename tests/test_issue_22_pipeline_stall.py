"""Regression tests for issue #22.

A staged pipeline stopped advancing after a stage's child exited. The
supervisor stayed up and kept heartbeating with zero children, so every
liveness check read healthy while nothing ran — 13 idle GPU-hours in the
reported case.

Root cause: the monitor's exit edge was
``state.last_exit_code != status.exit_code``, compared against a field
that **persists across Monitor instances**. Each pipeline stage builds a
fresh Monitor over one shared ``state.json``, so stage N's clean exit
left ``last_exit_code: 0`` on disk and stage N+1's clean exit compared
equal. The edge never fired, ``_handle_exit`` never ran, the stage never
completed, and the loop span forever over a dead child. The same trap
caught any second ``autosentry run`` in a directory whose previous run
had exited 0.

Covered here:

- the edge fires on a clean exit even when state says ``last_exit_code:
  0`` (the actual bug);
- the edge is per-child: once per death, again after a restart;
- ``restart_always`` + clean exit actually relaunches instead of parking
  on a dead child (same wedge, different road: the ``exit_code`` detector
  is non-zero-only, so nothing downstream fires);
- the dead-child watchdog stops a supervisor that has nothing to
  supervise, so any future variant is loud rather than silent;
- state.json and ``autosentry probe`` expose child liveness + stage, so
  the wedge is detectable from outside without parsing logs.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

from autosentry.config import load_config
from autosentry.monitor import Monitor, StageContext
from autosentry.state import MonitorState, StateStore
from autosentry.supervisors.base import ProcessStatus


def _write_cfg(tmp_path: Path, *, lifecycle: str | None = None, grace: int | None = None) -> Path:
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
        body = body.replace('command: ["true"]\n', f'command: ["true"]\n  lifecycle: {lifecycle}\n')
    if grace is not None:
        body += f"monitor:\n  dead_child_grace_seconds: {grace}\n"
    cfg = tmp_path / "autosentry.yaml"
    cfg.write_text(body)
    return cfg


def _monitor(
    tmp_path: Path,
    monkeypatch,
    *,
    lifecycle: str | None = None,
    grace: int | None = None,
    stage: StageContext | None = None,
) -> Monitor:
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path, lifecycle=lifecycle, grace=grace))
    monitor = Monitor(cfg, stage=stage)
    monitor.supervisor = MagicMock()
    monitor._notify = MagicMock()
    return monitor


# ----- the bug: the exit edge was keyed on persisted state ----------------


def test_clean_exit_is_detected_even_when_state_already_says_exit_zero(
    tmp_path: Path, monkeypatch
) -> None:
    """The reported failure: stage N leaves ``last_exit_code: 0`` on disk,
    stage N+1's child also exits 0, and the exit must still register."""
    monkeypatch.chdir(tmp_path)
    # Stand in for what the previous stage persisted.
    store = StateStore(tmp_path / ".autosentry" / "state.json")
    store.save(MonitorState(last_exit_code=0))

    monitor = _monitor(tmp_path, monkeypatch)
    assert monitor.state.last_exit_code == 0  # loaded from the prior stage

    dead = ProcessStatus(running=False, pid=101, exit_code=0, started_at="2026-07-27T22:36:53Z")
    assert monitor._is_new_child_exit(dead) is True


def test_exit_edge_fires_once_per_child_and_again_after_a_restart(
    tmp_path: Path, monkeypatch
) -> None:
    monitor = _monitor(tmp_path, monkeypatch)
    first = ProcessStatus(running=False, pid=1, exit_code=0, started_at="2026-07-27T22:36:53Z")

    assert monitor._is_new_child_exit(first) is True
    # Same dead child on every subsequent tick — must not re-fire.
    assert monitor._is_new_child_exit(first) is False
    assert monitor._is_new_child_exit(first) is False

    # A live replacement is not an exit.
    running = ProcessStatus(running=True, pid=2, exit_code=None, started_at="2026-07-28T01:00:00Z")
    assert monitor._is_new_child_exit(running) is False

    # ...and when *that* child dies, the edge fires again — even with the
    # identical exit code, which is exactly what the old check couldn't do.
    second = ProcessStatus(running=False, pid=2, exit_code=0, started_at="2026-07-28T01:00:00Z")
    assert monitor._is_new_child_exit(second) is True
    assert monitor._is_new_child_exit(second) is False


def test_stage_completes_when_previous_stage_left_exit_zero(tmp_path: Path, monkeypatch) -> None:
    """End-to-end on the monitor loop: with a pre-seeded ``last_exit_code:
    0``, ``run()`` must return rather than spin. Pre-fix this looped
    forever."""
    monkeypatch.chdir(tmp_path)
    store = StateStore(tmp_path / ".autosentry" / "state.json")
    store.save(MonitorState(last_exit_code=0))

    cfg = load_config(_write_cfg(tmp_path))
    monitor = Monitor(cfg)
    monitor._notify = MagicMock()
    monitor.supervisor = MagicMock()
    started = ProcessStatus(
        running=True, pid=555, exit_code=None, started_at="2026-07-28T00:00:00Z"
    )
    exited = ProcessStatus(running=False, pid=555, exit_code=0, started_at="2026-07-28T00:00:00Z")
    monitor.supervisor.start.return_value = started
    # First status() call (inside _tick) sees it running; then it's dead.
    monitor.supervisor.status.side_effect = [started, *([exited] * 50)]
    monitor.supervisor.iter_log_lines.return_value = iter([None] * 50)

    assert monitor.run() == 0
    assert monitor.state.last_exit_code == 0
    assert monitor.state.child_running is False


# ----- restart_always: the same wedge by a different road -----------------


def test_restart_always_relaunches_after_a_clean_exit(tmp_path: Path, monkeypatch) -> None:
    """The default ``exit_code`` detector is non-zero-only, so a clean exit
    fires no detection and no healer runs. ``restart_always`` has to do the
    relaunch itself or the child stays dead under a live supervisor."""
    monitor = _monitor(tmp_path, monkeypatch, lifecycle="restart_always")
    monitor.supervisor.start.return_value = ProcessStatus(
        running=True, pid=777, started_at="2026-07-28T02:00:00Z"
    )

    assert monitor._handle_exit(0) is True
    monitor.supervisor.start.assert_called_once()
    assert monitor.state.pid == 777
    assert monitor.state.child_running is True


def test_restart_always_clean_exit_does_not_spend_the_unverified_budget(
    tmp_path: Path, monkeypatch
) -> None:
    """A service that exits 0 and gets relaunched hasn't failed at
    anything — the budget is the kill-switch for healers that can't land a
    fix, so a clean-exit relaunch is audited but not charged."""
    monitor = _monitor(tmp_path, monkeypatch, lifecycle="restart_always")
    monitor.supervisor.start.return_value = ProcessStatus(running=True, pid=778)

    for _ in range(3):
        assert monitor._handle_exit(0) is True

    assert monitor.state.restarts == 0
    assert monitor.state.restarts_total == 3
    assert len(monitor.state.restart_history) == 3


# ----- the backstop: a supervisor with nothing to supervise ---------------


def test_watchdog_leaves_a_running_child_alone(tmp_path: Path, monkeypatch) -> None:
    monitor = _monitor(tmp_path, monkeypatch, grace=1)
    alive = ProcessStatus(running=True, pid=9, started_at="2026-07-28T02:00:00Z")
    assert monitor._dead_child_watchdog_tripped(alive) is False
    assert monitor._dead_child_watchdog_tripped(alive) is False
    assert monitor._stop is False


def test_watchdog_stops_the_monitor_once_the_grace_elapses(tmp_path: Path, monkeypatch) -> None:
    monitor = _monitor(tmp_path, monkeypatch, grace=60)
    dead = ProcessStatus(running=False, pid=9, exit_code=0, started_at="2026-07-28T02:00:00Z")

    # First sighting only starts the clock.
    assert monitor._dead_child_watchdog_tripped(dead) is False
    assert monitor._stop is False

    # Still inside the grace window.
    assert monitor._dead_child_watchdog_tripped(dead) is False
    assert monitor._stop is False

    # Rewind the clock past the grace period.
    monitor._child_dead_since -= 61
    assert monitor._dead_child_watchdog_tripped(dead) is True
    assert monitor._stop is True
    # A wedge is a failure, not a stage that finished: never exit 0.
    assert monitor._final_exit_code == 1


def test_watchdog_clock_resets_when_a_child_comes_back(tmp_path: Path, monkeypatch) -> None:
    monitor = _monitor(tmp_path, monkeypatch, grace=60)
    dead = ProcessStatus(running=False, pid=9, exit_code=1, started_at="2026-07-28T02:00:00Z")
    alive = ProcessStatus(running=True, pid=10, started_at="2026-07-28T02:05:00Z")

    monitor._dead_child_watchdog_tripped(dead)
    monitor._child_dead_since -= 61
    monitor._dead_child_watchdog_tripped(alive)
    assert monitor._child_dead_since is None

    # The next death starts a fresh window rather than tripping immediately.
    assert monitor._dead_child_watchdog_tripped(dead) is False
    assert monitor._stop is False


def test_watchdog_can_be_disabled(tmp_path: Path, monkeypatch) -> None:
    monitor = _monitor(tmp_path, monkeypatch, grace=0)
    dead = ProcessStatus(running=False, pid=9, exit_code=0, started_at="2026-07-28T02:00:00Z")
    monitor._dead_child_watchdog_tripped(dead)
    monitor._child_dead_since -= 100_000
    assert monitor._dead_child_watchdog_tripped(dead) is False
    assert monitor._stop is False


# ----- detectability from outside ----------------------------------------


def test_state_records_child_liveness_alongside_the_heartbeat(tmp_path: Path, monkeypatch) -> None:
    """A heartbeat proves a thread is scheduling, nothing more. The wedge
    is only distinguishable from health if state says what's underneath."""
    monitor = _monitor(tmp_path, monkeypatch)
    alive = ProcessStatus(running=True, pid=9, started_at="2026-07-28T02:00:00Z")
    dead = ProcessStatus(running=False, pid=9, exit_code=0, started_at="2026-07-28T02:00:00Z")

    monitor._record_child_liveness(alive)
    assert monitor.state.child_running is True
    assert monitor.state.child_dead_since is None

    monitor._record_child_liveness(dead)
    assert monitor.state.child_running is False
    first_seen = monitor.state.child_dead_since
    assert first_seen is not None

    # Repeat sightings keep the original timestamp — it's "dead since",
    # not "dead as of the last tick".
    monitor._record_child_liveness(dead)
    assert monitor.state.child_dead_since == first_seen

    monitor._record_child_liveness(alive)
    assert monitor.state.child_dead_since is None


def test_stage_context_lands_in_state(tmp_path: Path, monkeypatch) -> None:
    monitor = _monitor(tmp_path, monkeypatch, stage=StageContext(name="sft", index=2, count=3))
    assert (monitor.state.stage, monitor.state.stage_index, monitor.state.stage_count) == (
        "sft",
        2,
        3,
    )


def test_pipeline_passes_stage_context_and_clears_it_at_the_end(
    tmp_path: Path, monkeypatch
) -> None:
    from autosentry.pipeline import PipelineRunner

    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            """\
            process:
              kind: local
              stages:
                - { name: pretrain, command: ["echo", "a"] }
                - { name: sft, command: ["echo", "b"] }
            state_path: ".autosentry/state.json"
            """
        )
    )
    cfg = load_config(cfg_path)

    seen: list[StageContext] = []

    class _StubMonitor:
        def __init__(self, cfg, *, stage=None) -> None:  # noqa: ANN001
            seen.append(stage)

        def run(self) -> int:
            return 0

    import autosentry.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "Monitor", _StubMonitor)
    assert PipelineRunner(cfg).run() == 0

    assert [(s.name, s.index, s.count) for s in seen] == [("pretrain", 1, 2), ("sft", 2, 2)]
    # A finished pipeline mustn't read as still parked on its last stage.
    final = StateStore(tmp_path / ".autosentry" / "state.json").load()
    assert final.stage is None
    assert final.stage_index is None


def test_probe_reports_a_wedged_monitor(tmp_path: Path, monkeypatch) -> None:
    """``pid_alive and not stale`` is exactly what a naive liveness check
    calls healthy — the probe has to name this case for an external
    watchdog to catch it."""
    import os
    from datetime import datetime, timedelta, timezone

    from autosentry.cli.commands.probe import _build_payload, _inject_prompt

    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path, grace=60))
    now = datetime.now(tz=timezone.utc)
    StateStore(tmp_path / ".autosentry" / "state.json").save(
        MonitorState(
            pid=os.getpid(),  # a genuinely live pid, like the wedged supervisor
            last_heartbeat=now.isoformat(),  # ...still heartbeating
            child_running=False,  # ...over nothing
            child_dead_since=(now - timedelta(hours=13)).isoformat(),
            last_exit_code=0,
            stage="log124M_fineweb_fp8",
            stage_index=2,
            stage_count=3,
        )
    )

    payload = _build_payload(cfg)
    monitor = payload["monitor"]
    assert monitor["pid_alive"] is True
    assert monitor["stale"] is False  # the heartbeat is fine — that's the point
    assert monitor["wedged"] is True
    assert monitor["child_running"] is False
    assert monitor["child_dead_seconds"] > 46_000
    assert monitor["stage"] == "log124M_fineweb_fp8"
    assert "WEDGED" in payload["summary"]

    injected = _inject_prompt(payload)
    assert injected is not None
    assert injected["decision"] == "block"
    assert "WEDGED" in injected["reason"]


def test_probe_healthy_monitor_is_not_reported_wedged(tmp_path: Path, monkeypatch) -> None:
    import os
    from datetime import datetime, timezone

    from autosentry.cli.commands.probe import _build_payload

    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path, grace=60))
    now = datetime.now(tz=timezone.utc)
    StateStore(tmp_path / ".autosentry" / "state.json").save(
        MonitorState(pid=os.getpid(), last_heartbeat=now.isoformat(), child_running=True)
    )

    monitor = _build_payload(cfg)["monitor"]
    assert monitor["wedged"] is False
    assert monitor["child_running"] is True
    assert monitor["child_dead_seconds"] is None


def test_probe_schema_version_bumped_for_the_new_fields() -> None:
    from autosentry.cli.commands.probe import PROBE_SCHEMA_VERSION

    assert PROBE_SCHEMA_VERSION == 2


def test_state_json_round_trips_the_new_fields(tmp_path: Path) -> None:
    """External watchdogs read state.json directly; the fields have to
    survive a save/load cycle under their documented names."""
    path = tmp_path / "state.json"
    StateStore(path).save(
        MonitorState(
            child_running=False,
            child_started_at="2026-07-27T22:36:53Z",
            child_dead_since="2026-07-28T03:38:00Z",
            stage="sft",
            stage_index=2,
            stage_count=3,
        )
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["child_running"] is False
    assert raw["child_dead_since"] == "2026-07-28T03:38:00Z"
    assert raw["stage"] == "sft"
    assert StateStore(path).load().stage_index == 2
