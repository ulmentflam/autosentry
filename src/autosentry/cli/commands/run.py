"""``autosentry run`` — start the monitor."""

from __future__ import annotations

from pathlib import Path

import typer

from autosentry.cli import app
from autosentry.config import load_config
from autosentry.monitor import Monitor


@app.command()
def run(
    config: Path = typer.Option(  # noqa: B008
        Path("autosentry.yaml"),
        "--config",
        "-c",
        help="Path to autosentry.yaml. Defaults to ./autosentry.yaml.",
    ),
) -> None:
    """Start the supervisor and the detection loop.

    Foreground process: launches the configured `process.command`,
    streams its log lines through the configured detectors, and applies
    the matching healer's action when one fires. Blocks until SIGINT /
    SIGTERM, until `state.restarts` exhausts the budget, or until a
    Slack/Discord inbox command (`abort`) requests shutdown.

    For long-running deployments, launch in the background:

        nohup autosentry run > /dev/null 2>&1 &
        tail -F .autosentry/logs/autosentry.log

    For a live operator dashboard, run `autosentry watch` in a second
    pane.
    """
    cfg = load_config(config)
    Monitor(cfg).run()
