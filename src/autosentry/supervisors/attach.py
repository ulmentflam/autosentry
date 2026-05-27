"""Attach supervisor.

Doesn't launch anything; observes an existing process by PID and tails a
log file produced by some other system. The use case is monitoring a
service started by ``systemd`` / ``launchd`` / k8s where autosentry only
owns the observation plane.

Configuration:

.. code-block:: yaml

    process:
      kind: attach
      command: []
      extra:
        pid: 41822                              # OR pid_file: "/var/run/x.pid"
        log_path: "/var/log/myservice.log"
        allow_kill: false                       # apply_action(abort) sends SIGTERM only if true

Restart actions raise — attach mode can't restart a process it didn't
start. The healer layer is expected to either generate ``abort`` (if
``allow_kill`` is set) or ``custom_command`` actions.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from collections import deque
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from autosentry._tail import follow_file
from autosentry.config import AutoSentryConfig, RuleAction
from autosentry.logger import log
from autosentry.supervisors.base import LogLine, ProcessStatus, Supervisor, SupervisorError


class AttachSupervisor(Supervisor):
    def __init__(self, cfg: AutoSentryConfig, *, log_dir: Path) -> None:
        super().__init__(cfg, log_dir=log_dir)
        self.extra = cfg.process.extra or {}
        self._pid: int | None = _resolve_pid(self.extra)
        self._log_path: Path | None = _resolve_log_path(cfg, self.extra)
        self._started_at: str | None = None
        self._buf: deque[LogLine] = deque(maxlen=10_000)
        self._stop = threading.Event()
        # Signaled once the tail thread has opened the log file and seeked to
        # EOF; start() waits on this to avoid a race where the caller appends
        # before we've positioned, causing newly written lines to be swallowed.
        self._tail_ready = threading.Event()
        self._tail_thread: threading.Thread | None = None
        self._allow_kill: bool = bool(self.extra.get("allow_kill", False))

    # ----- lifecycle ---------------------------------------------------------

    def start(self) -> ProcessStatus:
        if self._pid is None:
            msg = "attach supervisor needs process.extra.pid or pid_file"
            raise SupervisorError(msg)
        if not _pid_alive(self._pid):
            log().error(f"attach: pid {self._pid} is not alive")
            return ProcessStatus(running=False, pid=self._pid)
        self._started_at = datetime.now(tz=timezone.utc).isoformat()
        log().action(f"attaching to pid={self._pid} log={self._log_path}")
        self._stop.clear()
        self._tail_ready.clear()
        if self._log_path is not None:
            self._tail_thread = threading.Thread(
                target=self._pump_log_file,
                name="autosentry-attach-tail",
                daemon=True,
            )
            self._tail_thread.start()
            # Wait briefly for the tail thread to open the file and seek to
            # EOF. Bounded so a missing log file doesn't hang start().
            self._tail_ready.wait(timeout=2.0)
        return self.status()

    def stop(self, *, timeout: float = 30.0) -> ProcessStatus:
        # We don't own the process. Just stop tailing.
        self._stop.set()
        if self._tail_thread is not None:
            self._tail_thread.join(timeout=5)
        return self.status()

    def status(self) -> ProcessStatus:
        if self._pid is None:
            return ProcessStatus(running=False)
        return ProcessStatus(
            running=_pid_alive(self._pid),
            pid=self._pid,
            started_at=self._started_at,
        )

    # ----- log streaming -----------------------------------------------------

    def iter_log_lines(self) -> Iterator[LogLine | None]:
        while not self._stop.is_set():
            if self._buf:
                yield self._buf.popleft()
            else:
                time.sleep(0.25)
                yield None

    def _pump_log_file(self) -> None:
        """Follow the watched log file. Seeks to EOF on first open so we don't
        replay potentially huge historical logs for a long-running service."""
        follow_file(
            path_provider=lambda: self._log_path,
            on_line=lambda line: self._buf.append(LogLine(text=line)),
            stop_event=self._stop,
            ready_event=self._tail_ready,
            seek_to_end=True,
            error_tag="attach",
        )

    # ----- actions -----------------------------------------------------------

    def apply_action(self, action: RuleAction) -> ProcessStatus:
        # Attach mode can't restart something it didn't start; everything else
        # delegates to the base. We also intercept ``abort`` to gate it on
        # ``extra.allow_kill`` and to send SIGTERM to the watched pid.
        if action.kind in ("restart", "restart_with_env"):
            msg = (
                "attach supervisor cannot restart a process it didn't start. "
                "Either configure a non-attach kind, or use `custom_command` "
                "to invoke your own restart logic."
            )
            raise SupervisorError(msg)
        if action.kind == "abort":
            if not self._allow_kill:
                log().error("attach: abort requested but extra.allow_kill is false — ignoring")
                return self.status()
            if self._pid is None:
                return self.status()
            log().action(f"attach: sending SIGTERM to pid {self._pid}")
            try:
                os.kill(self._pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            return self.status()
        return super().apply_action(action)


# ----- helpers --------------------------------------------------------------


def _resolve_pid(extra: dict) -> int | None:
    pid = extra.get("pid")
    if pid is not None:
        try:
            return int(pid)
        except (TypeError, ValueError) as e:
            msg = f"invalid extra.pid: {pid!r}"
            raise SupervisorError(msg) from e
    pid_file = extra.get("pid_file")
    if pid_file:
        try:
            content = Path(pid_file).read_text(encoding="utf-8").strip()
            return int(content.split()[0]) if content else None
        except (OSError, ValueError) as e:
            log().error(f"attach: couldn't read pid_file {pid_file}: {e}")
            return None
    return None


def _resolve_log_path(cfg: AutoSentryConfig, extra: dict) -> Path | None:
    raw = extra.get("log_path")
    if not raw:
        return None
    return cfg.resolve(raw)


def _pid_alive(pid: int) -> bool:
    """Cheap liveness check via ``kill(pid, 0)`` semantics."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # We exist, but can't signal — still counts as "alive".
        return True
    return True
