"""Structured logger matching the original rad_monitor.py format.

Line format: ``[2026-05-26 14:32:10 UTC] [LEVEL] message``

Levels are domain-specific, not Python stdlib levels: ``INFO``, ``ANOMALY``,
``RECOVERY``, ``ACTION``, ``CLAUDE``, ``SLACK``, ``ERROR``. They serve as
greppable prefixes for downstream tooling and for humans tailing the log.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, ClassVar

LEVELS = ("INFO", "ANOMALY", "RECOVERY", "ACTION", "CLAUDE", "SLACK", "ERROR")


class SentryLogger:
    """Append-only logger that fans out to stdout and a file."""

    _instance: ClassVar[SentryLogger | None] = None

    def __init__(self, log_path: Path | None = None, also_stdout: bool = True) -> None:
        self.log_path = log_path
        self.also_stdout = also_stdout
        self._lock = threading.Lock()
        self._file: IO[str] | None = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = log_path.open("a", encoding="utf-8")

    @classmethod
    def configure(cls, log_path: Path | None = None, also_stdout: bool = True) -> SentryLogger:
        if cls._instance is not None and cls._instance._file is not None:
            cls._instance._file.close()
        cls._instance = cls(log_path=log_path, also_stdout=also_stdout)
        return cls._instance

    @classmethod
    def get(cls) -> SentryLogger:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _emit(self, level: str, message: str) -> None:
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        line = f"[{ts}] [{level}] {message}\n"
        with self._lock:
            if self.also_stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
            if self._file is not None:
                self._file.write(line)
                self._file.flush()

    def info(self, message: str) -> None:
        self._emit("INFO", message)

    def anomaly(self, message: str) -> None:
        self._emit("ANOMALY", message)

    def recovery(self, message: str) -> None:
        self._emit("RECOVERY", message)

    def action(self, message: str) -> None:
        self._emit("ACTION", message)

    def claude(self, message: str) -> None:
        self._emit("CLAUDE", message)

    def slack(self, message: str) -> None:
        self._emit("SLACK", message)

    def error(self, message: str) -> None:
        self._emit("ERROR", message)

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


def log() -> SentryLogger:
    return SentryLogger.get()
