"""Local subprocess supervisor.

Runs the configured command with :class:`subprocess.Popen`, merging
stderr into stdout so we have a single ordered log stream. Lines are
pumped onto an internal queue by a background reader thread so the
monitor's main loop can iterate without blocking on PIPE reads.

The local supervisor also writes every line to
``<log_dir>/process.log`` for after-the-fact inspection — incident
reports excerpt from this file rather than re-tailing the pipe.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from autosentry.config import AutoSentryConfig
from autosentry.logger import log
from autosentry.supervisors.base import LogLine, ProcessStatus, Supervisor, SupervisorError

_SENTINEL = object()  # placed on the queue when the reader thread exits


class LocalSupervisor(Supervisor):
    def __init__(self, cfg: AutoSentryConfig, *, log_dir: Path) -> None:
        super().__init__(cfg, log_dir=log_dir)
        self._proc: subprocess.Popen[str] | None = None
        self._log_file: IO[str] | None = None
        self._reader: threading.Thread | None = None
        self._queue: queue.Queue[LogLine | object] = queue.Queue(maxsize=10000)
        self._started_at: str | None = None

    # ----- lifecycle ---------------------------------------------------------

    def start(self) -> ProcessStatus:
        if self._proc is not None and self._proc.poll() is None:
            return self.status()

        if not self.cfg.process.command:
            msg = "process.command is empty"
            raise SupervisorError(msg)

        env = os.environ.copy()
        env.update(self.cfg.process.env)
        env.update(self._env_overrides)

        cwd = self.cfg.resolve(self.cfg.process.cwd)
        log_path = self.log_dir / "process.log"
        self._log_file = log_path.open("a", encoding="utf-8", buffering=1)
        self._log_file.write(
            f"\n===== autosentry: start {datetime.now(tz=timezone.utc).isoformat()} =====\n"
        )

        log().action(f"Starting process: {' '.join(self.cfg.process.command)} (cwd={cwd})")

        self._proc = subprocess.Popen(  # noqa: S603
            self.cfg.process.command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._started_at = datetime.now(tz=timezone.utc).isoformat()

        self._reader = threading.Thread(
            target=self._pump_lines,
            args=(self._proc.stdout, self._log_file),
            name="autosentry-reader",
            daemon=True,
        )
        self._reader.start()
        return self.status()

    def stop(self, *, timeout: float = 30.0) -> ProcessStatus:
        if self._proc is None or self._proc.poll() is not None:
            return self.status()

        log().action(f"Stopping process pid={self._proc.pid} (SIGTERM, timeout={timeout}s)")
        try:
            # send to the whole group so child workers go too
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass

        deadline = time.monotonic() + timeout
        while self._proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        if self._proc.poll() is None:
            log().action(f"SIGTERM timed out, escalating to SIGKILL on pid={self._proc.pid}")
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._proc.wait(timeout=5)

        if self._log_file is not None:
            self._log_file.write(
                f"===== autosentry: stop {datetime.now(tz=timezone.utc).isoformat()} "
                f"exit={self._proc.returncode} =====\n"
            )
            self._log_file.flush()
        return self.status()

    def status(self) -> ProcessStatus:
        if self._proc is None:
            return ProcessStatus(running=False)
        rc = self._proc.poll()
        return ProcessStatus(
            running=rc is None,
            pid=self._proc.pid,
            exit_code=rc,
            started_at=self._started_at,
        )

    # ----- log streaming -----------------------------------------------------

    def _pump_lines(self, stream: IO[str], log_file: IO[str]) -> None:
        try:
            for raw in stream:
                line = raw.rstrip("\n")
                log_file.write(raw)
                try:
                    self._queue.put(LogLine(text=line), timeout=1.0)
                except queue.Full:
                    # If the consumer is so far behind we can't enqueue,
                    # drop the oldest item and try again. We'd rather see
                    # the most recent line than block stdout.
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._queue.put_nowait(LogLine(text=line))
                    except queue.Full:
                        pass
        finally:
            self._queue.put(_SENTINEL)

    def iter_log_lines(self) -> Iterator[LogLine | None]:
        """Yield log lines as they arrive; yield ``None`` on quiet ticks."""
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                yield None
                continue
            if item is _SENTINEL:
                return
            yield item  # type: ignore[misc]

    # ----- actions -----------------------------------------------------------

    def _apply_env_overrides(self, set_map: dict[str, str]) -> None:
        """``half`` / ``double`` keywords resolve against the current overlay
        (then the process environment) for a numeric base. Plain values
        are merged literally — matching the base class's behavior."""
        for k, v in set_map.items():
            if v in ("half", "double"):
                base = self._env_overrides.get(k) or os.environ.get(k)
                try:
                    base_int = int(base) if base is not None else None
                except ValueError:
                    base_int = None
                if base_int is None:
                    log().error(f"env_set {v} for {k}: no numeric base found in overlay or environ")
                    continue
                self._env_overrides[k] = (
                    str(max(1, base_int // 2)) if v == "half" else str(base_int * 2)
                )
            else:
                self._env_overrides[k] = v
