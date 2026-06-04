"""autosentry CLI.

The CLI is composed out of small per-command modules under
``autosentry.cli.commands``. This file owns the top-level Typer app
and the eager ``--version`` callback; everything else is registered
via :func:`_register_commands` so we don't grow a god-file again.
"""

from __future__ import annotations

import typer

from autosentry import __version__
from autosentry.cli.style import banner, console


def _print_version(value: bool) -> None:
    if not value:
        return
    console.print(banner(__version__))
    raise typer.Exit()


app = typer.Typer(
    name="autosentry",
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Self-healing supervisor for long-running processes.\n\n"
        "autosentry supervises a command, watches its log stream for known "
        "failure modes and anomalies, applies deterministic YAML rules when "
        "it can, and escalates to a Claude Code subagent when it can't. "
        "Each event becomes a folder under .autosentry/incidents/ with "
        "exploded stack frames, config snapshots, and the fix that was "
        "applied. Start with `autosentry init`, then `autosentry doctor`, "
        "then `autosentry run`."
    ),
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)


@app.callback()
def _main(
    version: bool = typer.Option(  # noqa: B008 — Typer idiom
        False,
        "--version",
        "-V",
        help="Print version and exit.",
        is_flag=True,
        callback=_print_version,
        is_eager=True,
    ),
) -> None:
    """autosentry — self-healing supervisor for long-running processes."""


def _register_commands() -> None:
    """Import each command module so it can attach itself to ``app``.

    Lazy attachment via import keeps the top-level entry fast (no
    network, no file IO) until the user actually runs a subcommand.
    """
    # Import order is for help output ordering only; otherwise no constraint.
    from autosentry.cli.commands import (  # noqa: F401
        analyze,
        dispatcher,
        doctor,
        healer,
        hooks,
        incidents,
        init,
        onboard,
        probe,
        reset,
        run,
        session,
        skills,
        status,
        update,
        watch,
        web,
    )


_register_commands()


__all__ = ["app"]
