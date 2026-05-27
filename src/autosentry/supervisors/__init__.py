"""Process supervisors.

Each supervisor knows how to start, observe, and stop a long-running
process. The monitor talks to a supervisor through the
:class:`~autosentry.supervisors.base.Supervisor` protocol; concrete
implementations encapsulate the lifecycle of one runtime
(local subprocess, SLURM job, Docker container, attached PID).
"""

from autosentry.supervisors.base import LogLine, ProcessStatus, Supervisor, SupervisorError
from autosentry.supervisors.local import LocalSupervisor


def supervisor_for(cfg, *, log_dir):  # pyrefly: ignore[bad-argument-type]
    """Factory: return the right supervisor for ``cfg.process.kind``."""
    kind = cfg.process.kind
    if kind == "local":
        return LocalSupervisor(cfg, log_dir=log_dir)
    if kind == "slurm":
        from autosentry.supervisors.slurm import SlurmSupervisor

        return SlurmSupervisor(cfg, log_dir=log_dir)
    if kind == "docker":
        from autosentry.supervisors.docker import DockerSupervisor

        return DockerSupervisor(cfg, log_dir=log_dir)
    if kind == "attach":
        from autosentry.supervisors.attach import AttachSupervisor

        return AttachSupervisor(cfg, log_dir=log_dir)
    msg = f"unknown supervisor kind: {kind}"
    raise SupervisorError(msg)


__all__ = [
    "LocalSupervisor",
    "LogLine",
    "ProcessStatus",
    "Supervisor",
    "SupervisorError",
    "supervisor_for",
]
