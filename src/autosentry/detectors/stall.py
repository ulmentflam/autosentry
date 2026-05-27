"""Stall detector — fires when progress hasn't advanced for too long.

Two stall flavors:

1. If ``metric_regex`` is supplied (e.g. ``r"step (\\d+)/"``), the detector
   tracks the captured value. When the value stops advancing for
   ``no_progress_seconds``, fire an anomaly.

2. If ``metric_regex`` is None, the detector falls back to "no log
   output at all" — fires when no line has been seen for
   ``no_progress_seconds``.
"""

from __future__ import annotations

import re
import time
from collections import deque

from autosentry.detectors.base import Detection, Detector
from autosentry.supervisors.base import LogLine

_CTX = 50


class StallDetector(Detector):
    def __init__(
        self,
        *,
        name: str = "stall",
        metric_regex: str | None = None,
        no_progress_seconds: int = 1800,
        cooldown_seconds: int = 600,
    ) -> None:
        super().__init__(name=name, cooldown_seconds=cooldown_seconds)
        self.metric_regex = re.compile(metric_regex) if metric_regex else None
        self.no_progress_seconds = no_progress_seconds
        self._last_progress_value: str | None = None
        self._last_progress_at: float = time.monotonic()
        self._last_line_at: float = time.monotonic()
        self._buf: deque[str] = deque(maxlen=_CTX)

    def observe_line(self, line: LogLine) -> Detection | None:
        self._buf.append(line.text)
        self._last_line_at = time.monotonic()
        if self.metric_regex is not None:
            m = self.metric_regex.search(line.text)
            if m:
                value = m.group(1) if m.groups() else m.group(0)
                if value != self._last_progress_value:
                    self._last_progress_value = value
                    self._last_progress_at = time.monotonic()
        return None

    def observe_tick(self) -> Detection | None:
        if self._on_cooldown():
            return None
        now = time.monotonic()
        # Pick the right "last activity" reference depending on mode.
        last_activity = (
            self._last_progress_at if self.metric_regex is not None else self._last_line_at
        )
        elapsed = now - last_activity
        if elapsed < self.no_progress_seconds:
            return None
        self._mark_fired()
        mode = "no progress" if self.metric_regex else "no output"
        msg = (
            f"stall: {mode} for {int(elapsed)}s (value={self._last_progress_value!r})"
            if self.metric_regex
            else f"stall: no output for {int(elapsed)}s"
        )
        meta = {"elapsed_seconds": str(int(elapsed))}
        if self._last_progress_value:
            meta["last_value"] = self._last_progress_value
        return Detection(
            detector=self.name,
            kind="anomaly",
            message=msg,
            log_context=list(self._buf),
            meta=meta,
        )
