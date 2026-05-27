"""Docker supervisor.

Runs the target inside a Docker container, tails ``docker logs -f`` for
the stream, and restarts by stopping + removing + re-running.

Configuration looks like:

.. code-block:: yaml

    process:
      kind: docker
      command:
        - docker
        - run
        - --detach
        - --name
        - autosentry-train
        - myimage:latest
      extra:
        container_name: "autosentry-train"
        log_stream_command: ["docker", "logs", "-f", "--tail", "0", "{name}"]
        stop_command:       ["docker", "stop", "--time", "30", "{name}"]
        remove_command:     ["docker", "rm", "-f", "{name}"]

The ``container_name`` is the single source of truth — we don't try to
discover IDs from the run command. If ``container_name`` is omitted we
generate ``autosentry-<8 hex chars>``. The user is expected to either
include ``--name {name}`` in their run command or to template it in.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import threading
import time
from collections import deque
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from autosentry.config import AutoSentryConfig
from autosentry.logger import log
from autosentry.supervisors.base import LogLine, ProcessStatus, Supervisor, SupervisorError


class DockerSupervisor(Supervisor):
    def __init__(self, cfg: AutoSentryConfig, *, log_dir: Path) -> None:
        super().__init__(cfg, log_dir=log_dir)
        self.extra = cfg.process.extra or {}
        self._container_name: str = (
            self.extra.get("container_name") or f"autosentry-{secrets.token_hex(4)}"
        )
        self._proc: subprocess.Popen[str] | None = None
        self._log_file: IO[str] | None = None
        self._reader: threading.Thread | None = None
        self._queue: deque[LogLine] = deque(maxlen=10_000)
        self._started_at: str | None = None
        self._stop = threading.Event()

    # ----- lifecycle ---------------------------------------------------------

    def start(self) -> ProcessStatus:
        if self._proc is not None and self._proc.poll() is None:
            return self.status()

        if not self.cfg.process.command:
            msg = "process.command is empty (need a `docker run …` invocation)"
            raise SupervisorError(msg)

        env = os.environ.copy()
        env.update(self.cfg.process.env)
        env.update(self._env_overrides)
        cwd = self.cfg.resolve(self.cfg.process.cwd)

        # Run the user's docker-run command. Expected to be `docker run -d …`
        # so the container survives after this returns.
        cmd = self._substitute_name(self.cfg.process.command)
        log().action(f"docker run: {' '.join(cmd)} (cwd={cwd})")
        result = subprocess.run(  # noqa: S603
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            msg = (
                f"docker run exited {result.returncode}: "
                f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
            )
            raise SupervisorError(msg)

        self._started_at = datetime.now(tz=timezone.utc).isoformat()
        log().action(f"container {self._container_name} started")

        log_path = self.log_dir / "process.log"
        self._log_file = log_path.open("a", encoding="utf-8", buffering=1)
        self._log_file.write(
            f"\n===== autosentry: docker container {self._container_name} "
            f"started {self._started_at} =====\n"
        )

        self._stop.clear()
        self._reader = threading.Thread(
            target=self._pump_logs,
            name="autosentry-docker-logs",
            daemon=True,
        )
        self._reader.start()
        return self.status()

    def stop(self, *, timeout: float = 30.0) -> ProcessStatus:
        if not self._is_alive():
            self._stop.set()
            return self.status()

        stop_cmd = self.extra.get("stop_command") or [
            "docker",
            "stop",
            "--time",
            str(int(timeout)),
            "{name}",
        ]
        resolved = self._substitute_name(stop_cmd)
        log().action(f"docker stop: {' '.join(resolved)}")
        subprocess.run(  # noqa: S603
            resolved,
            capture_output=True,
            text=True,
            check=False,
        )

        remove_cmd = self.extra.get("remove_command") or [
            "docker",
            "rm",
            "-f",
            "{name}",
        ]
        subprocess.run(  # noqa: S603
            self._substitute_name(remove_cmd),
            capture_output=True,
            text=True,
            check=False,
        )

        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=5)
        return self.status()

    def status(self) -> ProcessStatus:
        if not self._is_alive():
            return ProcessStatus(running=False)
        return ProcessStatus(
            running=True,
            pid=None,
            started_at=self._started_at,
        )

    # ----- log streaming -----------------------------------------------------

    def iter_log_lines(self) -> Iterator[LogLine | None]:
        while not self._stop.is_set():
            if self._queue:
                yield self._queue.popleft()
            else:
                time.sleep(0.25)
                yield None

    def _pump_logs(self) -> None:
        log_stream = self.extra.get("log_stream_command") or [
            "docker",
            "logs",
            "-f",
            "--tail",
            "0",
            "{name}",
        ]
        cmd = self._substitute_name(log_stream)
        try:
            self._proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as e:
            log().error(f"docker logs command not found: {e}")
            return
        try:
            stream = self._proc.stdout
            if stream is None:
                return
            for raw in stream:
                if self._stop.is_set():
                    break
                line = raw.rstrip("\n")
                if self._log_file is not None:
                    self._log_file.write(raw)
                self._queue.append(LogLine(text=line))
        finally:
            if self._proc is not None:
                try:
                    self._proc.terminate()
                except ProcessLookupError:
                    pass

    # ----- internals ---------------------------------------------------------

    def _substitute_name(self, args: list[str]) -> list[str]:
        return [a.replace("{name}", self._container_name) for a in args]

    def _is_alive(self) -> bool:
        """Ask ``docker inspect`` whether the container is still running."""
        try:
            result = subprocess.run(  # noqa: S603
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.Running}}",
                    self._container_name,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    # public read-only accessor for tests
    @property
    def container_name(self) -> str:
        return self._container_name
