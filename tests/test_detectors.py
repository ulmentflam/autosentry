"""Detector unit tests."""

from __future__ import annotations

from autosentry.detectors.exit_code import ExitCodeDetector
from autosentry.detectors.pattern import PatternDetector
from autosentry.detectors.stall import StallDetector
from autosentry.detectors.traceback import TracebackDetector
from autosentry.supervisors.base import LogLine, ProcessStatus


def _line(text: str) -> LogLine:
    return LogLine(text=text)


def test_pattern_detector_fires():
    d = PatternDetector(name="oom", regex=r"OutOfMemoryError")
    assert d.observe_line(_line("starting…")) is None
    det = d.observe_line(_line("torch.cuda.OutOfMemoryError: 2 GiB"))
    assert det is not None
    assert det.detector == "oom"
    assert "OutOfMemoryError" in det.message


def test_pattern_detector_cooldown():
    d = PatternDetector(name="oom", regex=r"OOM", cooldown_seconds=999)
    assert d.observe_line(_line("OOM 1")) is not None
    assert d.observe_line(_line("OOM 2")) is None  # cooldown


def test_python_traceback_detector():
    d = TracebackDetector()
    lines = [
        "starting…",
        "Traceback (most recent call last):",
        '  File "train.py", line 42, in step',
        "    out = self.model(x)",
        '  File "model.py", line 10, in forward',
        "    return self.layer(x)",
        "RuntimeError: shape mismatch",
        "more logs after",
    ]
    detection = None
    for ln in lines:
        d_out = d.observe_line(_line(ln))
        if d_out is not None:
            detection = d_out
            break
    assert detection is not None
    assert detection.meta.get("lang") == "python"
    assert "RuntimeError" in (detection.trace or "")


def test_node_traceback_detector():
    d = TracebackDetector()
    lines = [
        "doing things",
        "TypeError: Cannot read property 'x' of undefined",
        "    at Object.<anonymous> (/app/src/index.js:12:5)",
        "    at Module._compile (node:internal/modules/cjs/loader:1101:14)",
        "",
        "tail",
    ]
    detection = None
    for ln in lines:
        out = d.observe_line(_line(ln))
        if out is not None:
            detection = out
            break
    assert detection is not None
    assert detection.meta.get("lang") == "javascript"


def test_exit_code_detector():
    d = ExitCodeDetector()
    # running — no detection
    assert d.observe_status(ProcessStatus(running=True, pid=1)) is None
    det = d.observe_status(ProcessStatus(running=False, exit_code=139))
    assert det is not None
    assert "139" in det.message
    # doesn't double-fire for same exit
    assert d.observe_status(ProcessStatus(running=False, exit_code=139)) is None


def test_stall_detector_no_output():
    # no metric_regex → "no output" mode. Use 0s threshold to force fire on tick.
    d = StallDetector(no_progress_seconds=0, cooldown_seconds=0)
    # Manually backdate so the tick fires.
    import time

    d._last_line_at = time.monotonic() - 10
    det = d.observe_tick()
    assert det is not None
    assert det.kind == "anomaly"


def test_stall_detector_tracks_metric_progress():
    d = StallDetector(metric_regex=r"step\s+(\d+)/", no_progress_seconds=1800)
    d.observe_line(_line("step 9/312841 | loss 3.2"))
    assert d._last_progress_value == "9"


def test_stall_detector_resets_on_child_restart():
    """Issue #9: on restart the tracked value and no-progress clock must
    reset so a spurious stall doesn't fire on the healthy replacement."""
    import time

    d = StallDetector(metric_regex=r"step\s+(\d+)/", no_progress_seconds=1800)
    d.observe_line(_line("step 9/312841 | loss 3.2"))
    # Simulate the old child having gone quiet long past the threshold.
    d._last_progress_at = time.monotonic() - 5000
    d._last_line_at = time.monotonic() - 5000
    # Without a reset this tick would fire a (spurious) stall.
    assert d.observe_tick() is not None

    # Restart: the detector must forget the dead child's frozen value and
    # restart its no-progress clock from now.
    d.on_child_restart()
    assert d._last_progress_value is None
    assert time.monotonic() - d._last_progress_at < 1.0
    assert time.monotonic() - d._last_line_at < 1.0

    # A fresh child that is making progress must not trip the detector: its
    # step counter restarts from 1 (< the old value of 9), which the
    # value-change tracking still counts as progress.
    d._last_fired_at = float("-inf")  # clear cooldown from the spurious fire
    d.observe_line(_line("step 1/19552 | loss 4.1"))
    assert d._last_progress_value == "1"
    assert d.observe_tick() is None
