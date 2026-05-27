"""Exit-code detector — fires when the supervised process exits."""

from __future__ import annotations

from collections import deque

from autosentry.detectors.base import Detection, Detector
from autosentry.supervisors.base import LogLine, ProcessStatus

_CTX = 100


class ExitCodeDetector(Detector):
    def __init__(
        self, *, name: str = "exit_code", nonzero_only: bool = True, cooldown_seconds: int = 60
    ) -> None:
        super().__init__(name=name, cooldown_seconds=cooldown_seconds)
        self.nonzero_only = nonzero_only
        self._buf: deque[str] = deque(maxlen=_CTX)
        self._fired_for_exit: int | None = None

    def observe_line(self, line: LogLine) -> Detection | None:
        self._buf.append(line.text)
        return None

    def observe_status(self, status: ProcessStatus) -> Detection | None:
        if status.running or status.exit_code is None:
            self._fired_for_exit = None
            return None
        if self.nonzero_only and status.exit_code == 0:
            return None
        # only fire once per exit
        if self._fired_for_exit == status.exit_code:
            return None
        self._fired_for_exit = status.exit_code
        return Detection(
            detector=self.name,
            kind="error",
            message=f"process exited with code {status.exit_code}",
            log_context=list(self._buf),
            meta={"exit_code": str(status.exit_code)},
        )
