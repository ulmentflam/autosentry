"""Phase 5.5 — healer-aware restart budget.

Tests the three behaviors:
1. ``restarts_total`` persists across kept-fix resets
2. ``state.restarts`` resets to 0 on a kept verification
3. Escalation flag flips on at the threshold, clears on kept fix,
   and forces the next detection to skip the rule healer
"""

from __future__ import annotations

import time
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

from autosentry.config import load_config
from autosentry.healers.base import HealerOutcome
from autosentry.monitor import Monitor


def _write_cfg(tmp_path: Path, **claude_overrides) -> Path:
    """Minimal config; ``claude.enabled=false`` so we don't accidentally
    spawn anything during tests."""
    cfg = tmp_path / "autosentry.yaml"
    cfg.write_text(
        dedent(
            """\
            process:
              kind: local
              command: ["true"]
              restart_policy:
                max_restarts: 4
            healing:
              claude:
                enabled: false
              escalate_to_claude_after: 2
            state_path: ".autosentry/state.json"
            incidents_dir: ".autosentry/incidents"
            """
        )
    )
    return cfg


def _make_monitor(tmp_path: Path, monkeypatch) -> Monitor:
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path))
    monitor = Monitor(cfg)
    # Avoid spawning the real supervisor in any test.
    monitor.supervisor = MagicMock()
    return monitor


# ----- counter semantics ---------------------------------------------------


def test_record_restart_increments_both_counters(tmp_path: Path, monkeypatch):
    monitor = _make_monitor(tmp_path, monkeypatch)
    monitor.state.record_restart(reason="x")
    monitor.state.record_restart(reason="y")
    assert monitor.state.restarts == 2
    assert monitor.state.restarts_total == 2


def test_state_load_tolerates_missing_restarts_total(tmp_path: Path):
    """Pre-Phase-5.5 state files lack ``restarts_total``."""
    from autosentry.state import StateStore

    state_path = tmp_path / "state.json"
    state_path.write_text('{"pid": 42, "restarts": 3}')
    store = StateStore(state_path)
    state = store.load()
    # Default falls back to 0; the field exists going forward.
    assert state.restarts_total == 0
    assert state.restarts == 3


# ----- reset-on-kept ------------------------------------------------------


def test_tick_verifications_resets_restarts_on_kept(tmp_path: Path, monkeypatch):
    monitor = _make_monitor(tmp_path, monkeypatch)
    # Simulate two unverified restarts.
    monitor.state.record_restart(reason="a")
    monitor.state.record_restart(reason="b")
    assert monitor.state.restarts == 2
    assert monitor.state.restarts_total == 2

    # Plant a pending verification whose deadline has already passed.
    monitor._pending_verify["detX"] = (time.monotonic() - 1, "inc-1", "rule_a", None, 1)
    # Seed a matching ledger row so the update() call has something to find.
    from autosentry.ledger import Attempt

    monitor.attempts.append(
        Attempt(
            timestamp="2026-01-01T00:00:00+00:00",
            incident_id="inc-1",
            detector="detX",
            source="rule_a",
            branch="",
            status="pending",
            duration_seconds=0.0,
            description="x",
        )
    )

    monitor._tick_verifications()
    assert monitor.state.restarts == 0
    # All-time counter is unchanged.
    assert monitor.state.restarts_total == 2


def test_tick_verifications_clears_escalation_flag(tmp_path: Path, monkeypatch):
    monitor = _make_monitor(tmp_path, monkeypatch)
    monitor._escalation_active = True
    monitor._pending_verify["detX"] = (time.monotonic() - 1, "inc-1", "rule_a", None, 1)
    from autosentry.ledger import Attempt

    monitor.attempts.append(
        Attempt(
            timestamp="2026-01-01T00:00:00+00:00",
            incident_id="inc-1",
            detector="detX",
            source="rule_a",
            branch="",
            status="pending",
            duration_seconds=0.0,
            description="x",
        )
    )
    monitor._tick_verifications()
    assert monitor._escalation_active is False


# ----- escalation flip ----------------------------------------------------


def test_escalation_threshold_default_is_half_max_restarts(tmp_path: Path, monkeypatch):
    monitor = _make_monitor(tmp_path, monkeypatch)
    # Config explicitly sets escalate_to_claude_after=2; max_restarts=4.
    assert monitor._escalation_threshold == 2


def test_escalation_threshold_default_decoupled_from_max_restarts(tmp_path: Path, monkeypatch):
    """The default escalation threshold is a literal 2, independent of
    ``max_restarts``. Decoupling matters now that the default
    ``max_restarts`` is high (50): if we still computed ``// 5``,
    Claude wouldn't take over until restart 10 — rules would
    monopolize the first half of the kill-switch budget. Literal 2
    keeps the agentic flow as the main fix path."""
    monkeypatch.chdir(tmp_path)
    yaml = tmp_path / "autosentry.yaml"
    yaml.write_text(
        dedent(
            """\
            process:
              kind: local
              command: ["true"]
              restart_policy:
                max_restarts: 50
            healing:
              claude:
                enabled: false
            """
        )
    )
    cfg = load_config(yaml)
    monitor = Monitor(cfg)
    monitor.supervisor = MagicMock()
    assert monitor._escalation_threshold == 2


def test_escalation_threshold_still_2_when_max_restarts_is_small(tmp_path: Path, monkeypatch):
    """Even with a tight kill-switch (max_restarts=3), escalation
    still fires at 2 unverified restarts. The old behavior floored
    ``3 // 5 = 0`` to 1; the new literal-2 default holds across small
    and large caps alike — no special-case logic needed."""
    monkeypatch.chdir(tmp_path)
    yaml = tmp_path / "autosentry.yaml"
    yaml.write_text(
        dedent(
            """\
            process:
              kind: local
              command: ["true"]
              restart_policy:
                max_restarts: 3
            healing:
              claude:
                enabled: false
            """
        )
    )
    cfg = load_config(yaml)
    monitor = Monitor(cfg)
    monitor.supervisor = MagicMock()
    assert monitor._escalation_threshold == 2


def test_maybe_flip_escalation_flips_at_threshold(tmp_path: Path, monkeypatch):
    monitor = _make_monitor(tmp_path, monkeypatch)
    monitor.state.restarts = 1
    monitor._maybe_flip_escalation()
    assert monitor._escalation_active is False

    monitor.state.restarts = 2  # at threshold
    monitor._maybe_flip_escalation()
    assert monitor._escalation_active is True


def test_maybe_flip_escalation_only_notifies_once(tmp_path: Path, monkeypatch):
    monitor = _make_monitor(tmp_path, monkeypatch)
    calls: list[tuple] = []
    monitor._notify = lambda *a, **kw: calls.append((a, kw))  # type: ignore[assignment]
    monitor.state.restarts = 5

    monitor._maybe_flip_escalation()
    monitor._maybe_flip_escalation()
    monitor._maybe_flip_escalation()
    assert len(calls) == 1


# ----- force-Claude path --------------------------------------------------


def test_active_escalation_skips_rule_healer(tmp_path: Path, monkeypatch):
    """When the escalation flag is on, _fire_detection must route to the
    Claude healer directly — even when a rule WOULD have matched."""
    monitor = _make_monitor(tmp_path, monkeypatch)
    monitor._escalation_active = True

    from autosentry.config import RuleAction
    from autosentry.detectors.base import Detection

    rule_healer_called = False
    claude_healer_called = False

    def fake_rule_attempt(det):
        nonlocal rule_healer_called
        rule_healer_called = True
        return HealerOutcome(action=RuleAction(kind="restart"), source="rule", rule_name="r1")

    def fake_claude_attempt(det, **kwargs):
        nonlocal claude_healer_called
        claude_healer_called = True
        return HealerOutcome(action=RuleAction(kind="restart"), source="claude")

    monitor.rule_healer.attempt = fake_rule_attempt  # type: ignore[method-assign]
    monitor.claude_healer.attempt = fake_claude_attempt  # type: ignore[method-assign]

    monitor._fire_detection(Detection(detector="x", kind="error", message="hi"))

    assert claude_healer_called is True
    assert rule_healer_called is False


def test_inactive_escalation_uses_rule_first(tmp_path: Path, monkeypatch):
    """Baseline — without escalation, rule healer fires first as before."""
    monitor = _make_monitor(tmp_path, monkeypatch)
    monitor._escalation_active = False

    from autosentry.config import RuleAction
    from autosentry.detectors.base import Detection

    rule_called = False
    claude_called = False

    def fake_rule_attempt(det):
        nonlocal rule_called
        rule_called = True
        return HealerOutcome(action=RuleAction(kind="restart"), source="rule", rule_name="r1")

    def fake_claude_attempt(det, **kwargs):
        nonlocal claude_called
        claude_called = True
        return HealerOutcome(action=RuleAction(kind="restart"), source="claude")

    monitor.rule_healer.attempt = fake_rule_attempt  # type: ignore[method-assign]
    monitor.claude_healer.attempt = fake_claude_attempt  # type: ignore[method-assign]

    monitor._fire_detection(Detection(detector="x", kind="error", message="hi"))

    assert rule_called is True
    assert claude_called is False  # rule matched, no fallback needed


def test_rule_regression_forces_claude_on_next_attempt(tmp_path: Path, monkeypatch):
    """A rule-based fix that regresses should pivot the next attempt
    straight to Claude — rules already failed, default posture is to
    bring in the agent."""
    monitor = _make_monitor(tmp_path, monkeypatch)
    monitor._escalation_active = False

    from autosentry.config import RuleAction
    from autosentry.detectors.base import Detection

    det = Detection(detector="detX", kind="error", message="hi")
    # Stage a pending verification for detX sourced from rules.
    monitor._pending_verify["detX"] = (
        time.monotonic() + 600,
        "inc-1",
        "rules",
        None,
        1,
    )

    rule_called = False
    claude_called = False

    def fake_rule_attempt(d):
        nonlocal rule_called
        rule_called = True
        return HealerOutcome(action=RuleAction(kind="restart"), source="rule", rule_name="r1")

    def fake_claude_attempt(d, **kwargs):
        nonlocal claude_called
        claude_called = True
        return HealerOutcome(action=RuleAction(kind="restart"), source="claude")

    monitor.rule_healer.attempt = fake_rule_attempt  # type: ignore[method-assign]
    monitor.claude_healer.attempt = fake_claude_attempt  # type: ignore[method-assign]

    monitor._fire_detection(det)

    # Rule regressed → Claude takes over on this very attempt; the
    # marker clears after use so a subsequent fresh detection goes
    # back through rules.
    assert claude_called is True
    assert rule_called is False
    assert "detX" not in monitor._force_claude_next


def test_rule_regression_escalation_disabled_keeps_rules(tmp_path: Path, monkeypatch):
    """With escalate_on_rule_regression=False, a regression doesn't
    force Claude on the next attempt."""
    monitor = _make_monitor(tmp_path, monkeypatch)
    monitor._escalation_active = False
    monitor.cfg.healing.escalate_on_rule_regression = False

    from autosentry.config import RuleAction
    from autosentry.detectors.base import Detection

    det = Detection(detector="detX", kind="error", message="hi")
    monitor._pending_verify["detX"] = (
        time.monotonic() + 600,
        "inc-1",
        "rules",
        None,
        1,
    )

    rule_called = False

    def fake_rule_attempt(d):
        nonlocal rule_called
        rule_called = True
        return HealerOutcome(action=RuleAction(kind="restart"), source="rule", rule_name="r1")

    monitor.rule_healer.attempt = fake_rule_attempt  # type: ignore[method-assign]
    monitor.claude_healer.attempt = lambda d, **kw: None  # type: ignore[method-assign]

    monitor._fire_detection(det)

    assert rule_called is True
    assert "detX" not in monitor._force_claude_next


# ----- restart_policy fallback (issue #4) ----------------------------------


def _stub_supervisor_dead(monitor) -> None:
    """Wire the mocked supervisor to look like the child died with code 1."""
    from autosentry.supervisors.base import ProcessStatus

    monitor.supervisor.status = MagicMock(
        return_value=ProcessStatus(running=False, pid=60740, exit_code=1)
    )
    monitor.supervisor.start = MagicMock(return_value=ProcessStatus(running=True, pid=60741))
    # Zero the cooldown so the test doesn't actually sleep 60s.
    monitor.cfg.process.restart_policy.cooldown_seconds = 0


def test_no_recovery_falls_back_to_restart_policy_when_child_dead(tmp_path: Path, monkeypatch):
    """Regression for issue #4: when the child exits, no rule matches, and
    the Claude healer returns no action (timeout / disabled), the monitor
    must exercise the restart_policy budget instead of wheel-spinning over
    a dead child. The pre-fix behavior was a 90% CPU loop with no restart,
    no exit, and no state transition until a human sent SIGTERM."""
    from autosentry.detectors.base import Detection

    monitor = _make_monitor(tmp_path, monkeypatch)
    _stub_supervisor_dead(monitor)
    # No rule + Claude disabled in cfg ⇒ outcome is None.
    monitor.rule_healer.attempt = lambda d: None  # type: ignore[method-assign]
    monitor.claude_healer.attempt = lambda d, **kw: None  # type: ignore[method-assign]

    monitor._fire_detection(Detection(detector="exit_code", kind="error", message="exit 1"))

    monitor.supervisor.start.assert_called_once()
    assert monitor.state.restarts == 1
    assert monitor.state.pid == 60741
    assert monitor.state.last_exit_code is None  # cleared so next exit is a fresh transition
    assert monitor._stop is False


def test_no_recovery_stops_monitor_when_max_restarts_reached(tmp_path: Path, monkeypatch):
    """Once the restart_policy budget is exhausted, the fallback must
    stop the monitor (set _stop) so the outer run() loop exits cleanly
    — instead of leaving a wheel-spinning supervisor for a service manager
    to chase."""
    from autosentry.detectors.base import Detection

    monitor = _make_monitor(tmp_path, monkeypatch)
    _stub_supervisor_dead(monitor)
    monitor.state.restarts = monitor.state.max_restarts  # at the limit
    monitor.rule_healer.attempt = lambda d: None  # type: ignore[method-assign]
    monitor.claude_healer.attempt = lambda d, **kw: None  # type: ignore[method-assign]

    monitor._fire_detection(Detection(detector="exit_code", kind="error", message="exit 1"))

    monitor.supervisor.start.assert_not_called()
    assert monitor._stop is True


def test_no_recovery_no_fallback_when_child_still_running(tmp_path: Path, monkeypatch):
    """The fallback is for dead children. An anomaly detection that fires
    while the supervised process is healthy must NOT trigger a restart —
    that would be a regression in its own right."""
    from autosentry.detectors.base import Detection
    from autosentry.supervisors.base import ProcessStatus

    monitor = _make_monitor(tmp_path, monkeypatch)
    monitor.supervisor.status = MagicMock(return_value=ProcessStatus(running=True, pid=60750))
    monitor.supervisor.start = MagicMock()
    monitor.rule_healer.attempt = lambda d: None  # type: ignore[method-assign]
    monitor.claude_healer.attempt = lambda d, **kw: None  # type: ignore[method-assign]

    monitor._fire_detection(Detection(detector="stall", kind="anomaly", message="quiet"))

    monitor.supervisor.start.assert_not_called()
    assert monitor.state.restarts == 0
    assert monitor._stop is False
