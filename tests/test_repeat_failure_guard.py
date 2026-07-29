"""Consecutive-identical-failure guard.

``max_restarts`` is a budget for how many times a fix may fail to stick, and
it cannot distinguish a transient from a deterministic error because both
merely decrement it. So a config mistake burns the entire budget one identical
failure at a time.

The reported case: a pipeline stage failed with ``pixi: command not found``
(exit 127 — a PATH problem that could never resolve itself) and retried 55
times over roughly 14 hours, each attempt re-running a completed job's
10-minute evaluation step.

These tests pin the three behaviours that matter:

* an identical failure repeating past the cap stops the supervisor, whatever
  ``max_restarts`` still allows;
* a *different* failure resets the streak, so an occasional error interleaved
  with real progress is never mistaken for a deterministic one;
* the guard is off when the cap is 0, preserving the previous behaviour for
  anyone who wants it.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from autosentry.config import load_config
from autosentry.detectors.base import Detection
from autosentry.monitor import Monitor


def _cfg(tmp_path: Path, cap: int) -> Path:
    p = tmp_path / "autosentry.yaml"
    p.write_text(
        dedent(
            f"""\
            process:
              kind: local
              command: ["true"]
              restart_policy:
                max_restarts: 50
                max_identical_failures: {cap}
            healing:
              claude:
                enabled: false
            state_path: ".autosentry/state.json"
            incidents_dir: ".autosentry/incidents"
            """
        )
    )
    return p


def _monitor(tmp_path: Path, cap: int) -> Monitor:
    return Monitor(load_config(_cfg(tmp_path, cap)))


def _det(message: str, detector: str = "exit_code") -> Detection:
    return Detection(detector=detector, kind="error", message=message)


def test_identical_failures_trip_the_guard_at_the_cap(tmp_path: Path) -> None:
    m = _monitor(tmp_path, cap=3)
    d = _det("process exited with code 127")
    # Up to and including the cap the guard stays quiet: a fault that repeats
    # a few times may still be a slow-healing transient.
    assert [m._note_repeat(d) for _ in range(3)] == [False, False, False]
    # Past it, every subsequent detection reports exhausted.
    assert m._note_repeat(d) is True
    assert m._note_repeat(d) is True


def test_a_different_failure_resets_the_streak(tmp_path: Path) -> None:
    """Interleaved progress means the failure is not deterministic.

    This is the guard's whole safety property: without it, a flaky error
    appearing occasionally among successful restarts would eventually trip
    the cap and stop a supervisor that was recovering perfectly well.
    """
    m = _monitor(tmp_path, cap=2)
    same = _det("process exited with code 127")
    other = _det("CUDA call failed")
    assert m._note_repeat(same) is False
    assert m._note_repeat(same) is False
    assert m._note_repeat(other) is False  # streak broken, counter restarts
    assert m._note_repeat(same) is False
    assert m._note_repeat(same) is False
    assert m._note_repeat(same) is True  # three in a row past a cap of 2


def test_varying_numbers_do_not_defeat_the_guard(tmp_path: Path) -> None:
    """The signature collapses digits.

    The failure this guard was written for carried a changing pid and
    timestamp on every retry. Comparing raw message text would have treated
    all 55 occurrences as distinct and never fired.
    """
    m = _monitor(tmp_path, cap=2)
    for i in range(3):
        got = m._note_repeat(_det(f"pid {1000 + i}: pixi: command not found"))
    assert got is True


def test_different_detectors_are_never_the_same_failure(tmp_path: Path) -> None:
    m = _monitor(tmp_path, cap=1)
    msg = "process exited with code 1"
    # Same message text, different detector: the switch resets the streak, so
    # the stall detection starts its own run rather than inheriting the
    # exit_code one.
    assert m._note_repeat(_det(msg, detector="exit_code")) is False
    assert m._note_repeat(_det(msg, detector="stall")) is False
    assert m._note_repeat(_det(msg, detector="stall")) is True


def test_cap_zero_disables_the_guard(tmp_path: Path) -> None:
    m = _monitor(tmp_path, cap=0)
    d = _det("process exited with code 127")
    assert [m._note_repeat(d) for _ in range(20)] == [False] * 20


def test_default_cap_is_set(tmp_path: Path) -> None:
    """A guard that ships off by default protects nobody who hasn't already
    been bitten, which is exactly the population that needs it."""
    p = tmp_path / "autosentry.yaml"
    p.write_text(
        dedent(
            """\
            process:
              kind: local
              command: ["true"]
            state_path: ".autosentry/state.json"
            incidents_dir: ".autosentry/incidents"
            """
        )
    )
    cfg = load_config(p)
    assert cfg.process.restart_policy.max_identical_failures > 0
