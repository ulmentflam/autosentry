"""Self-update logic for autosentry.

Detects how the CLI was installed (uv tool / pipx / pip --user / unknown)
and dispatches to the corresponding upgrade command. ``--check`` queries
PyPI for the latest version and reports current vs latest without
touching anything.

The detection is best-effort. If we can't tell how autosentry got here,
we shell out to ``install.sh`` (downloaded fresh from GitHub) as the
canonical fallback path — same mechanism the user can use by hand.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from autosentry import __version__

Method = Literal["uv", "pipx", "pip", "unknown"]

_PYPI_JSON = "https://pypi.org/pypi/autosentry/json"
_INSTALL_SH_URL = "https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh"


@dataclass(frozen=True)
class UpdateCheck:
    current: str
    latest: str
    is_outdated: bool


def detect_install_method() -> Method:
    """Guess how this autosentry got onto the box.

    We look at the running interpreter's path and at well-known tool
    install layouts. Order matters: uv tool layout is most specific,
    pipx next, then anything else is treated as plain ``pip``.
    """
    py = Path(sys.executable).resolve()
    home = Path.home()

    # uv tool installs land under: $HOME/.local/share/uv/tools/<name>/...
    if "uv/tools" in str(py) or "uv\\tools" in str(py):
        return "uv"
    # pipx: $HOME/.local/pipx/venvs/<name>/...
    if "pipx/venvs" in str(py) or "pipx\\venvs" in str(py):
        return "pipx"
    # Anything inside ~/.local that wasn't matched above looks like pip --user
    try:
        if home in py.parents and "/.local/" in str(py):
            return "pip"
    except ValueError:
        pass
    # Could still be pip in a virtualenv — but we don't want to auto-upgrade
    # a project's pinned venv on the user's behalf. Bail and let the caller
    # decide.
    return "unknown"


def fetch_latest_version(*, allow_pre: bool = False, timeout: int = 10) -> str:
    """Return the latest version string for autosentry from PyPI.

    With ``allow_pre=False``, skip pre-release / dev versions.
    Raises :class:`RuntimeError` on any network or parsing failure.
    """
    try:
        with urllib.request.urlopen(_PYPI_JSON, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        msg = f"couldn't reach PyPI: {e}"
        raise RuntimeError(msg) from e

    if allow_pre:
        return payload["info"]["version"]

    # Walk through release keys and pick the highest stable version.
    releases = payload.get("releases", {}) or {}
    stable = [v for v in releases if _is_stable(v) and releases[v]]
    if not stable:
        return payload["info"]["version"]
    return max(stable, key=_version_key)


def _is_stable(v: str) -> bool:
    lowered = v.lower()
    return not any(marker in lowered for marker in ("a", "b", "rc", ".dev", "+", "pre"))


def _version_key(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in v.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check(*, allow_pre: bool = False) -> UpdateCheck:
    latest = fetch_latest_version(allow_pre=allow_pre)
    return UpdateCheck(
        current=__version__,
        latest=latest,
        is_outdated=_version_key(latest) > _version_key(__version__),
    )


def perform_update(
    *,
    method: Method | None = None,
    allow_pre: bool = False,
    version: str | None = None,
) -> int:
    """Run the appropriate upgrade command.

    Returns the subprocess exit code (0 on success). For ``unknown``, we
    fetch the canonical ``install.sh`` and execute it; that script is
    idempotent and behaves as an upgrade-in-place.
    """
    chosen: Method = method or detect_install_method()
    spec = "autosentry" if not version else f"autosentry=={version}"
    pre = ["--pre"] if allow_pre else []

    if chosen == "uv":
        if not shutil.which("uv"):
            return _fallback_to_install_sh(allow_pre=allow_pre, version=version)
        return _run(["uv", "tool", "upgrade", *pre, "autosentry"])
    if chosen == "pipx":
        if not shutil.which("pipx"):
            return _fallback_to_install_sh(allow_pre=allow_pre, version=version)
        return _run(["pipx", "upgrade", *pre, "autosentry"])
    if chosen == "pip":
        py = os.environ.get("AUTOSENTRY_PYTHON") or sys.executable
        return _run([py, "-m", "pip", "install", "--user", "--upgrade", *pre, spec])
    return _fallback_to_install_sh(allow_pre=allow_pre, version=version)


def _run(cmd: list[str]) -> int:
    try:
        return subprocess.call(cmd)  # noqa: S603 — explicit list, no shell
    except FileNotFoundError as e:
        print(f"error: {cmd[0]} not found: {e}", file=sys.stderr)
        return 127


def _fallback_to_install_sh(*, allow_pre: bool, version: str | None) -> int:
    """Download install.sh from GitHub and run it.

    Honors the same env vars the install.sh respects, so the user's
    pre-flag or pinned version flows through.
    """
    env = os.environ.copy()
    if allow_pre:
        env["AUTOSENTRY_PRE"] = "1"
    if version:
        env["AUTOSENTRY_VERSION"] = version
    try:
        with urllib.request.urlopen(_INSTALL_SH_URL, timeout=30) as resp:  # noqa: S310
            script = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"error: couldn't fetch install.sh: {e}", file=sys.stderr)
        return 1
    completed = subprocess.run(  # noqa: S603
        ["sh", "-s"],
        input=script,
        text=True,
        env=env,
        check=False,
    )
    return completed.returncode
