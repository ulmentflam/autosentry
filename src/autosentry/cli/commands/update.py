"""``autosentry update`` — upgrade autosentry in place."""

from __future__ import annotations

import json
from typing import cast

import typer

from autosentry.cli import app
from autosentry.cli.style import DIM, ERR, INFO, OK, WARN, console
from autosentry.updater import (
    Method,
    detect_install_method,
    perform_update,
)
from autosentry.updater import (
    check as updater_check,
)


def _recommended_command() -> str:
    """The upgrade command to suggest, tailored to how autosentry was installed."""
    if detect_install_method() == "brew":
        return "brew upgrade autosentry"
    return "autosentry update"


@app.command()
def update(
    check_only: bool = typer.Option(False, "--check", help="Check for an update; do not install."),
    json_out: bool = typer.Option(
        False, "--json", help="Machine-readable check output (implies --check)."
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Ignore the cached result and query PyPI live."
    ),
    pre: bool = typer.Option(False, "--pre", help="Allow pre-release versions."),
    version: str = typer.Option(
        "", "--version", "-V", help="Pin to a specific version (otherwise: latest)."
    ),
    method: str = typer.Option(
        "",
        "--method",
        help="Force install method: uv | pipx | pip | brew. Default: auto-detect.",
    ),
) -> None:
    """Upgrade the installed autosentry CLI to the latest PyPI release.

    Auto-detects the install method (uv tool, pipx, pip --user, Homebrew)
    and delegates to the matching upgrade command. Falls back to running
    install.sh from GitHub when the install method can't be determined.

    Use --check to compare against PyPI without installing; it recommends
    the right upgrade command when you're behind and exits 0 either way.
    The lookup is cached for a day (--no-cache forces a live query), and
    --json emits the result for scripts. --pre allows pre-releases,
    --version pins a release, --method forces the install backend.
    """
    if check_only or json_out:
        try:
            result = updater_check(allow_pre=pre, use_cache=not no_cache)
        except RuntimeError as e:
            if json_out:
                typer.echo(json.dumps({"error": str(e)}))
            else:
                console.print(f"[{ERR}]check failed:[/{ERR}] {e}")
            raise typer.Exit(code=1) from e

        if json_out:
            typer.echo(
                json.dumps(
                    {
                        "current": result.current,
                        "latest": result.latest,
                        "is_outdated": result.is_outdated,
                    }
                )
            )
            return

        if result.is_outdated:
            console.print(
                f"[bold]autosentry[/bold] {result.current}  "
                f"[{DIM}](latest: {result.latest})[/{DIM}]"
            )
            console.print(
                f"[{WARN}]→ update available — run [bold]{_recommended_command()}[/bold][/{WARN}]"
            )
        else:
            console.print(f"[{OK}]autosentry {result.current} — up to date[/{OK}]")
        return

    chosen_str = method or detect_install_method()
    valid_methods = {"uv", "pipx", "pip", "brew", "unknown"}
    if chosen_str not in valid_methods:
        console.print(
            f"[{ERR}]invalid --method '{chosen_str}'[/{ERR}] (choose: uv, pipx, pip, brew)"
        )
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
