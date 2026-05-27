"""``autosentry watch`` — live status TUI."""

from __future__ import annotations

from pathlib import Path

import typer

from autosentry.cli import app
from autosentry.cli.style import console
from autosentry.config import load_config


@app.command()
def watch(
    config: Path = typer.Option(  # noqa: B008
        Path("autosentry.yaml"),
        "--config",
        "-c",
        help="Path to autosentry.yaml. Defaults to ./autosentry.yaml.",
    ),
    refresh: float = typer.Option(
        1.0,
        "--refresh",
        help="Refresh interval in seconds (default 1.0). Min 0.25.",
    ),
    once: bool = typer.Option(
        False,
        "--once",
        help="Render a single frame and exit (no Live updates; useful in scripts).",
    ),
) -> None:
    """Continuously-refreshing operator dashboard for a running monitor.

    Four stacked panels: state snapshot (pid, uptime, restarts, last
    heartbeat), recent incidents from the index, per-detector
    last-fired times, and a color-coded tail of the structured log.
    Quit with Ctrl-C.

    Reads state purely from disk (`state.json`, `incidents/index.jsonl`,
    the log file), so it doesn't disturb the running monitor — safe to
    leave open in a second pane.
    """
    from autosentry.tui import run_tui

    cfg = load_config(config)
    run_tui(cfg, refresh=refresh, console=console, once=once)
