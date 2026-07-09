"""Detector protocol + shared types."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from autosentry.supervisors.base import LogLine, ProcessStatus

DetectionKind = Literal["error", "anomaly"]


@dataclass
class Detection:
    detector: str
    kind: DetectionKind
    message: str
    # Raw log context: lines leading up to (and including) the triggering line.
    log_context: list[str] = field(default_factory=list)
    # Stack trace as a single block if available (set by TracebackDetector).
    trace: str | None = None
    # Detector-specific metadata (e.g. stall info, regex group captures).
    meta: dict[str, str] = field(default_factory=dict)


class Detector(ABC):
    name: str
    cooldown_seconds: int

    def __init__(self, name: str, cooldown_seconds: int = 60) -> None:
        self.name = name
        self.cooldown_seconds = cooldown_seconds
        # `-inf` so a never-fired detector is never on cooldown, regardless
        # of the absolute value of `time.monotonic()` (which is boot-relative
        # and can be smaller than `cooldown_seconds` on fresh CI runners).
        self._last_fired_at: float = float("-inf")

    def _on_cooldown(self) -> bool:
        return (time.monotonic() - self._last_fired_at) < self.cooldown_seconds

    def _mark_fired(self) -> None:
        self._last_fired_at = time.monotonic()

    def on_child_restart(self) -> None:  # noqa: B027 — deliberate no-op hook; most detectors are stateless across restarts
        """Called when the supervised child is (re)started.

        Detectors that track per-child state (e.g. a stall detector's
        last-progress value and no-progress clock) must clear it here so
        the new child is observed from a clean slate rather than carrying
        the dead child's state forward (issue #9). The firing cooldown is
        intentionally *not* reset — it governs the detector's own alert
        rate, independent of which child is running. Default: no-op.
        """

    @abstractmethod
    def observe_line(self, line: LogLine) -> Detection | None:
        """Called for each log line. Return a Detection or None."""

    def observe_status(self, status: ProcessStatus) -> Detection | None:
        """Called on each monitor tick with current process status."""
        return None

    def observe_tick(self) -> Detection | None:
        """Called once per tick regardless of log activity (for time-based checks)."""
        return None
