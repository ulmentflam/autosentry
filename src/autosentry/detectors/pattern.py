"""Regex-pattern detector — the workhorse for known error keywords."""

from __future__ import annotations

import re
from collections import deque

from autosentry.detectors.base import Detection, Detector
from autosentry.supervisors.base import LogLine

_CTX = 50  # lines of log context kept for incident reports


class PatternDetector(Detector):
    """Fire when a log line matches ``regex``."""

    def __init__(self, *, name: str, regex: str, cooldown_seconds: int = 60) -> None:
        super().__init__(name=name, cooldown_seconds=cooldown_seconds)
        self.regex = re.compile(regex)
        self._buf: deque[str] = deque(maxlen=_CTX)

    def observe_line(self, line: LogLine) -> Detection | None:
        self._buf.append(line.text)
        if self._on_cooldown():
            return None
        m = self.regex.search(line.text)
        if not m:
            return None
        self._mark_fired()
        groups = {f"group{i}": g for i, g in enumerate(m.groups(), start=1) if g is not None}
        return Detection(
            detector=self.name,
            kind="error",
            message=f"pattern matched: {m.group(0)[:200]}",
            log_context=list(self._buf),
            meta=groups,
        )
