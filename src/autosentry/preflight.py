"""Check stage launch requirements using the child's working directory and PATH."""

from __future__ import annotations

import os
import shutil

from autosentry.config import AutoSentryConfig


def check_launch(cfg: AutoSentryConfig) -> list[str]:
    errors: list[str] = []
    cwd = cfg.resolve(cfg.process.cwd)
    if not cwd.is_dir():
        errors.append(f"Working directory does not exist: {cwd}")
    elif not os.access(cwd, os.X_OK):
        errors.append(f"Working directory is not accessible: {cwd}")
    env = {**os.environ, **cfg.process.env}
    # Relative PATH entries resolve against the child's cwd, not our cwd.
    search_path = os.pathsep.join(
        str(cwd / entry) for entry in env.get("PATH", os.defpath).split(os.pathsep)
    )
    commands = list(cfg.process.required_executables)
    if cfg.process.kind != "attach" and cfg.process.command:
        commands.insert(0, cfg.process.command[0])
    for command in dict.fromkeys(commands):
        candidate = str(cwd / command) if os.sep in command else command
        if not shutil.which(candidate, path=search_path):
            errors.append(f"Executable not found or not executable: {command!r} (check stage PATH)")
    for key in cfg.process.required_env:
        if not env.get(key):
            errors.append(f"Required environment variable is missing or empty: {key}")
    return errors
