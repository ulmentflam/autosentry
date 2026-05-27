"""Log-stream and process-state detectors.

A detector observes the log stream and the supervisor's status, and
emits :class:`Detection` events when something interesting happens.
Detectors are deliberately dumb — they describe *what was seen*, not
*what to do*. The healer layer decides.
"""

from autosentry.detectors.base import Detection, Detector
from autosentry.detectors.exit_code import ExitCodeDetector
from autosentry.detectors.pattern import PatternDetector
from autosentry.detectors.stall import StallDetector
from autosentry.detectors.traceback import TracebackDetector


def build_detectors(specs):
    """Instantiate detectors from ``DetectorSpec`` list."""
    out: list[Detector] = []
    for spec in specs:
        if spec.kind == "pattern":
            if not spec.regex:
                msg = f"pattern detector '{spec.name}' missing 'regex'"
                raise ValueError(msg)
            out.append(
                PatternDetector(
                    name=spec.name or f"pattern_{len(out)}",
                    regex=spec.regex,
                    cooldown_seconds=spec.cooldown_seconds,
                )
            )
        elif spec.kind == "traceback":
            out.append(
                TracebackDetector(
                    name=spec.name or "traceback",
                    cooldown_seconds=spec.cooldown_seconds,
                )
            )
        elif spec.kind == "stall":
            out.append(
                StallDetector(
                    name=spec.name or "stall",
                    metric_regex=spec.metric_regex,
                    no_progress_seconds=spec.no_progress_seconds or 1800,
                    cooldown_seconds=spec.cooldown_seconds,
                )
            )
        elif spec.kind == "exit_code":
            out.append(
                ExitCodeDetector(
                    name=spec.name or "exit_code",
                    nonzero_only=spec.nonzero_only,
                    cooldown_seconds=spec.cooldown_seconds,
                )
            )
    return out


__all__ = [
    "Detection",
    "Detector",
    "ExitCodeDetector",
    "PatternDetector",
    "StallDetector",
    "TracebackDetector",
    "build_detectors",
]
