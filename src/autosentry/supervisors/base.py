"""Supervisor protocol.

A supervisor is the smallest possible interface around a managed
process. Subclasses implement :meth:`start`, :meth:`stop`,
:meth:`status`, and :meth:`iter_log_lines`. The default
:meth:`apply_action` covers the universal ``pause`` / ``abort`` /
``custom_command`` branches and delegates ``restart`` /
``restart_with_env`` to small hooks (:meth:`_restart`,
:meth:`_apply_env_overrides`) that subclasses override only when their
semantics differ from a plain stop-then-start.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from autosentry.config import AutoSentryConfig, RuleAction
from autosentry.logger import log


class SupervisorError(RuntimeError):
    pass


@dataclass(frozen=True)
class LogLine:
    text: str
    stream: str = "stdout"  # 'stdout' | 'stderr' (merged streams emit 'stdout')


@dataclass
class ProcessStatus:
    running: bool
    pid: int | None = None
    exit_code: int | None = None
    started_at: str | None = None  # ISO-8601 UTC


class Supervisor(ABC):
    """Abstract base every supervisor implements."""

    cfg: AutoSentryConfig
    log_dir: Path

    def __init__(self, cfg: AutoSentryConfig, *, log_dir: Path) -> None:
        self.cfg = cfg
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # Env overrides applied on the next start; subclasses populate
        # via :meth:`_apply_env_overrides` from a ``restart_with_env``
        # action. The local supervisor uses these; SLURM and Docker push
        # them into their command's env at start time.
        self._env_overrides: dict[str, str] = {}

    @abstractmethod
    def start(self) -> ProcessStatus: ...

    @abstractmethod
    def stop(self, *, timeout: float = 30.0) -> ProcessStatus: ...

    @abstractmethod
    def status(self) -> ProcessStatus: ...

    @abstractmethod
    def iter_log_lines(self) -> Iterator[LogLine | None]: ...

    # ----- default action handling ----------------------------------------

    def apply_action(self, action: RuleAction) -> ProcessStatus:
        """Default action dispatch.

        Subclasses override the hooks (:meth:`_restart`,
        :meth:`_apply_env_overrides`) rather than this method. Attach-mode
        overrides this method to reject restart actions outright.
        """
        kind = action.kind
        if kind == "restart":
            return self._restart()
        if kind == "restart_with_env":
            self._apply_env_overrides(action.set)
            return self._restart()
        if kind == "pause":
            return self.stop()
        if kind == "abort":
            self.stop()
            return self.status()
        if kind == "custom_command":
            return self._run_custom_command(action)
        msg = f"unsupported action: {kind}"
        raise SupervisorError(msg)

    # ----- action hooks subclasses may override ---------------------------

    def _restart(self) -> ProcessStatus:
        """Default: stop, then start. Subclasses with smarter restart
        semantics (e.g. SLURM where ``stop`` must wait for the job to
        leave the queue) override the lifecycle methods themselves; this
        hook stays simple."""
        self.stop()
        return self.start()

    def _apply_env_overrides(self, set_map: dict[str, str]) -> None:
        """Merge a ``restart_with_env.set`` block into ``self._env_overrides``.

        Default: literal copy. ``LocalSupervisor`` overrides this to
        resolve ``half`` / ``double`` against the current overlay.
        """
        self._env_overrides.update(set_map)

    def _run_custom_command(self, action: RuleAction) -> ProcessStatus:
        if not action.command:
            msg = "custom_command action requires 'command'"
            raise SupervisorError(msg)
        log().action(f"Running custom_command: {' '.join(action.command)}")
        subprocess.run(  # noqa: S603
            action.command,
            cwd=str(self.cfg.resolve(self.cfg.process.cwd)),
            check=False,
        )
        return self.status()
