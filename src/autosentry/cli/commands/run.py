"""``autosentry run`` — start the monitor."""

from __future__ import annotations

from pathlib import Path

import typer

from autosentry.cli import app
from autosentry.config import DEFAULT_CONFIG_PATH, load_config
from autosentry.monitor import Monitor
from autosentry.pipeline import PipelineRunner


@app.command()
def run(
    config: Path = typer.Option(  # noqa: B008
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help="Path to the config. Defaults to .autosentry/autosentry.yaml "
        "(falls back to ./autosentry.yaml).",
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
    if cfg.process.is_pipeline():
        exit_code = PipelineRunner(cfg).run()
    else:
        exit_code = Monitor(cfg).run()
    # Propagate the child's exit code so ``one_shot`` and clean exits
    # under ``restart_on_failure`` surface correctly to a parent service
    # manager (issue #5).
    if exit_code:
        raise typer.Exit(code=exit_code)
