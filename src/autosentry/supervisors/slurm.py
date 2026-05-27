"""SLURM supervisor.

Submits jobs via ``sbatch``, polls ``squeue`` / ``sacct`` for status,
tails the log file SLURM writes for the job, and re-submits on restart.
This is the supervisor the original ``rad_monitor.py`` was built around,
ported to the generic abstraction.

Configuration lives under ``process.extra``; the keys mirror what the
original required:

.. code-block:: yaml

    process:
      kind: slurm
      command: ["sbatch", "slurm/submit_train.sh"]
      extra:
        log_pattern: "logs/job_{job_id}.log"
        status_command: ["squeue", "--noheader", "--format=%T", "-j", "{job_id}"]
        cancel_command: ["scancel", "{job_id}"]
        sacct_command:  ["sacct", "-X", "-n", "-o", "State", "-j", "{job_id}"]
        poll_interval_seconds: 10

We submit via ``cfg.process.command`` so a user can pass any sbatch
flags they want (``--export=…``, ``--dependency=afterok:N``, etc.) by
configuring the command itself. The supervisor doesn't try to abstract
sbatch — it just runs whatever the user asked for and remembers the
returned job id.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from autosentry._tail import follow_file
from autosentry.config import AutoSentryConfig
from autosentry.logger import log
from autosentry.supervisors.base import LogLine, ProcessStatus, Supervisor, SupervisorError

# Active SLURM job states we treat as "running" (process alive).
_RUNNING_STATES = {
    "PENDING",
    "PD",
    "RUNNING",
    "R",
    "CONFIGURING",
    "CG",
    "REQUEUED",
    "REQUEUE",
    "RESIZING",
    "SUSPENDED",
    "S",
}
# Terminal failure-ish states — we map these to non-zero exit codes.
_FAILED_STATES = {
    "FAILED",
    "F",
    "BOOT_FAIL",
    "BF",
    "NODE_FAIL",
    "NF",
    "TIMEOUT",
    "TO",
    "OUT_OF_MEMORY",
    "OOM",
    "CANCELLED",
    "CA",
    "DEADLINE",
    "DL",
    "PREEMPTED",
    "PR",
    "REVOKED",
    "RV",
}
_COMPLETED_STATES = {"COMPLETED", "CD"}

_JOB_ID_RE = re.compile(r"Submitted batch job\s+(\d+)")


class SlurmSupervisor(Supervisor):
    def __init__(self, cfg: AutoSentryConfig, *, log_dir: Path) -> None:
        super().__init__(cfg, log_dir=log_dir)
        self.extra = cfg.process.extra or {}
        self._job_id: str | None = None
        self._started_at: str | None = None
        self._last_known_state: str | None = None
        self._log_path: Path | None = None
        self._log_buf: deque[LogLine] = deque(maxlen=10_000)
        self._poll_interval = float(self.extra.get("poll_interval_seconds", 10))
        self._tail_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ----- lifecycle ---------------------------------------------------------

    def start(self) -> ProcessStatus:
        if self._job_id is not None:
            current = self.status()
            if current.running:
                return current

        if not self.cfg.process.command:
            msg = "process.command is empty (need an sbatch invocation)"
            raise SupervisorError(msg)

        env = os.environ.copy()
        env.update(self.cfg.process.env)
        env.update(self._env_overrides)
        cwd = self.cfg.resolve(self.cfg.process.cwd)

        log().action(f"sbatch: {' '.join(self.cfg.process.command)} (cwd={cwd})")
        result = subprocess.run(  # noqa: S603
            self.cfg.process.command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            msg = (
                f"sbatch exited {result.returncode}: "
                f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
            )
            raise SupervisorError(msg)

        m = _JOB_ID_RE.search(result.stdout)
        if not m:
            msg = f"could not parse job id from sbatch output: {result.stdout.strip()!r}"
            raise SupervisorError(msg)
        self._job_id = m.group(1)
        self._started_at = datetime.now(tz=timezone.utc).isoformat()
        log().action(f"sbatch submitted job {self._job_id}")

        # Resolve the log file path now (it may not exist yet — SLURM creates
        # it shortly after the job starts running; the tail thread will wait).
        log_pattern = self.extra.get("log_pattern")
        if log_pattern:
            self._log_path = self.cfg.resolve(_fmt(log_pattern, self._job_id))
        else:
            self._log_path = None

        self._stop.clear()
        self._tail_thread = threading.Thread(
            target=self._pump_log_file,
            name="autosentry-slurm-tail",
            daemon=True,
        )
        self._tail_thread.start()
        return self.status()

    def stop(self, *, timeout: float = 30.0) -> ProcessStatus:
        if self._job_id is None:
            return self.status()
        cancel_cmd = self.extra.get("cancel_command") or ["scancel", "{job_id}"]
        resolved = [_fmt(arg, self._job_id) for arg in cancel_cmd]
        log().action(f"scancel: {' '.join(resolved)}")
        subprocess.run(  # noqa: S603
            resolved,
            capture_output=True,
            text=True,
            check=False,
        )
        # Wait briefly for the job to leave the running set.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.status()
            if not current.running:
                break
            time.sleep(2)
        self._stop.set()
        if self._tail_thread is not None:
            self._tail_thread.join(timeout=5)
        return self.status()

    def status(self) -> ProcessStatus:
        if self._job_id is None:
            return ProcessStatus(running=False)
        state = self._query_state()
        self._last_known_state = state
        if state in _RUNNING_STATES:
            return ProcessStatus(
                running=True,
                pid=_pid_from_job(self._job_id),
                started_at=self._started_at,
            )
        if state in _COMPLETED_STATES:
            return ProcessStatus(
                running=False,
                pid=_pid_from_job(self._job_id),
                exit_code=0,
                started_at=self._started_at,
            )
        if state in _FAILED_STATES:
            return ProcessStatus(
                running=False,
                pid=_pid_from_job(self._job_id),
                exit_code=_exit_code_for_failed(state),
                started_at=self._started_at,
            )
        # Unknown state — treat as "still pending"; SLURM scrolls jobs through
        # transient states during scheduling and we don't want to spuriously
        # claim the job has exited.
        return ProcessStatus(running=True, pid=_pid_from_job(self._job_id))

    # ----- log streaming -----------------------------------------------------

    def iter_log_lines(self) -> Iterator[LogLine | None]:
        """Yield buffered log lines; ``None`` on quiet ticks.

        The tail thread fills the buffer in the background. The monitor
        consumes here at its own pace.
        """
        while not self._stop.is_set():
            if self._log_buf:
                yield self._log_buf.popleft()
            else:
                time.sleep(0.25)
                yield None

    def _pump_log_file(self) -> None:
        """Follow the SLURM log file, replaying from the start so historical
        output is captured (the job may have been running before the tail
        thread attached)."""
        follow_file(
            path_provider=lambda: self._log_path,
            on_line=lambda line: self._log_buf.append(LogLine(text=line)),
            stop_event=self._stop,
            seek_to_end=False,
            error_tag="slurm",
            missing_backoff_cap=10.0,
        )

    # ----- actions -----------------------------------------------------------

    def _restart(self) -> ProcessStatus:
        # SLURM needs the job id reset between submissions so start() re-submits
        # via sbatch instead of polling the old (now terminal) job.
        self.stop()
        self._job_id = None
        return self.start()

    # ----- internals ---------------------------------------------------------

    def _query_state(self) -> str:
        """Best-effort SLURM state query: squeue first, sacct as fallback."""
        if self._job_id is None:
            return ""
        status_cmd = self.extra.get("status_command") or [
            "squeue",
            "--noheader",
            "--format=%T",
            "-j",
            "{job_id}",
        ]
        out = _run_for_state([_fmt(a, self._job_id) for a in status_cmd])
        if out:
            return out

        # squeue empty → job has left the queue; ask sacct for the terminal state.
        sacct_cmd = self.extra.get("sacct_command") or [
            "sacct",
            "-X",
            "-n",
            "-o",
            "State",
            "-j",
            "{job_id}",
        ]
        return _run_for_state([_fmt(a, self._job_id) for a in sacct_cmd])


# ----- helpers --------------------------------------------------------------


def _fmt(template: str, job_id: str | None) -> str:
    """Format a SLURM template string. ``{job_id}`` is the only substitution."""
    if job_id is None:
        return template
    return template.replace("{job_id}", job_id)


def _run_for_state(cmd: list[str]) -> str:
    """Run a SLURM CLI command and return the first non-empty stdout line.

    Returns ``""`` on any failure — callers treat that as "no information".
    """
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""
    for line in (result.stdout or "").splitlines():
        cleaned = line.strip()
        if cleaned:
            # sacct returns trailing markers like ``COMPLETED ``; normalize.
            return cleaned.split()[0].upper()
    return ""


def _pid_from_job(job_id: str | None) -> int | None:
    """SLURM jobs don't have a single PID; we surface the job id as int."""
    if job_id is None:
        return None
    try:
        return int(job_id)
    except ValueError:
        return None


def _exit_code_for_failed(state: str) -> int:
    """Best-effort mapping from SLURM terminal state to a non-zero exit code."""
    mapping = {
        "TIMEOUT": 124,
        "TO": 124,
        "OUT_OF_MEMORY": 137,
        "OOM": 137,
        "NODE_FAIL": 1,
        "NF": 1,
        "CANCELLED": 130,
        "CA": 130,
    }
    return mapping.get(state.upper(), 1)
