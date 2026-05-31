"""``autosentry hooks`` — manage Claude Code hook installation.

Today this just wraps the Stop-hook installer for the session-dispatch
flow (see :mod:`autosentry.hooks`). The hook is installed automatically
by ``autosentry init`` at local scope; this command is for re-running
the install (e.g. after the operator deleted ``settings.local.json``)
or for tearing it down (``--remove``).
"""

from __future__ import annotations

from pathlib import Path

import typer

from autosentry.cli import app
from autosentry.cli.style import DIM, OK, WARN, console
from autosentry.hooks import (
    STOP_HOOK_COMMAND,
    install_claude_stop_hook,
    uninstall_claude_stop_hook,
)

hooks_app = typer.Typer(
    name="hooks",
    help="Install / uninstall the Claude Code Stop hook used by session dispatch.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(hooks_app, name="hooks")


@hooks_app.command("install")
def install(
    target: Path = typer.Argument(  # noqa: B008
        Path("."), help="Repo to install the hook into (default: cwd)."
    ),
) -> None:
    """Wire the session-dispatch Stop hook into ``.claude/settings.local.json``.

    Idempotent — re-running is safe. The hook calls
    ``autosentry probe --inject-prompt --quiet`` after every assistant
    turn; when there's pending work or the monitor is down, the probe's
    JSON output re-engages the model on the next turn.
    """
    result = install_claude_stop_hook(target)
    label = {
        "created": "created",
        "added": "added to existing settings",
        "already_present": "already present",
        "skipped": "skipped (settings file unreadable)",
    }[result.status]
    style = WARN if result.status == "skipped" else OK
    console.print(
        f"[{style}]✓[/{style}] Claude Code Stop hook: {label} "
        f"[{DIM}]({result.settings_path})[/{DIM}]"
    )
    console.print(f"  [{DIM}]command:[/{DIM}] {STOP_HOOK_COMMAND}")


@hooks_app.command("remove")
def remove(
    target: Path = typer.Argument(  # noqa: B008
        Path("."), help="Repo to remove the hook from (default: cwd)."
    ),
) -> None:
    """Remove the autosentry Stop hook from ``.claude/settings.local.json``.

    Leaves the settings file (and any other hooks the operator added) in
    place. No-op if the hook isn't installed.
    """
    if uninstall_claude_stop_hook(target):
        console.print(f"[{OK}]✓[/{OK}] Claude Code Stop hook removed.")
    else:
        console.print(f"[{DIM}]nothing to remove (hook not present).[/{DIM}]")
