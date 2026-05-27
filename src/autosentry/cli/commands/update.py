"""``autosentry update`` — upgrade autosentry in place."""

from __future__ import annotations

from typing import cast

import typer

from autosentry.cli import app
from autosentry.cli.style import ERR, INFO, OK, WARN, console
from autosentry.updater import (
    Method,
    detect_install_method,
    perform_update,
)
from autosentry.updater import (
    check as updater_check,
)


@app.command()
def update(
    check_only: bool = typer.Option(False, "--check", help="Check for an update; do not install."),
    pre: bool = typer.Option(False, "--pre", help="Allow pre-release versions."),
    version: str = typer.Option(
        "", "--version", "-V", help="Pin to a specific version (otherwise: latest)."
    ),
    method: str = typer.Option(
        "",
        "--method",
        help="Force install method: uv | pipx | pip. Default: auto-detect.",
    ),
) -> None:
    """Upgrade the installed autosentry CLI to the latest PyPI release.

    Auto-detects the install method (uv tool, pipx, pip --user) and
    delegates to the matching upgrade command. Falls back to running
    install.sh from GitHub when the install method can't be determined.

    Use --check to query PyPI without installing — exits non-zero
    when an update is available, useful in scripts that want to nag.
    --pre allows pre-release versions; --version pins to a specific
    release; --method forces the install backend.
    """
    if check_only:
        try:
            result = updater_check(allow_pre=pre)
        except RuntimeError as e:
            console.print(f"[{ERR}]check failed:[/{ERR}] {e}")
            raise typer.Exit(code=1) from e
        line = f"current: {result.current}  ·  latest: {result.latest}"
        if result.is_outdated:
            console.print(f"[{WARN}]{line}  ·  update available[/{WARN}]")
            raise typer.Exit(code=1)
        console.print(f"[{OK}]{line}  ·  up to date[/{OK}]")
        return

    chosen_str = method or detect_install_method()
    valid_methods = {"uv", "pipx", "pip", "unknown"}
    if chosen_str not in valid_methods:
        console.print(f"[{ERR}]invalid --method '{chosen_str}'[/{ERR}] (choose: uv, pipx, pip)")
        raise typer.Exit(code=2)
    chosen: Method | None = None if chosen_str == "unknown" else cast(Method, chosen_str)
    console.print(
        f"[bold]autosentry update[/bold] · method=[{INFO}]{chosen_str}[/{INFO}] · "
        f"pre={'yes' if pre else 'no'} · target={version or 'latest'}"
    )
    if chosen_str == "unknown":
        console.print(
            f"[{WARN}]couldn't detect install method; falling back to install.sh[/{WARN}]"
        )
    code = perform_update(method=chosen, allow_pre=pre, version=version or None)
    if code != 0:
        console.print(f"[{ERR}]update failed (exit {code})[/{ERR}]")
        raise typer.Exit(code=code)
    console.print(f"[{OK}]update complete[/{OK}]")
