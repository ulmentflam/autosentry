"""Export a pipeline evidence bundle for an existing agent conversation."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from autosentry.cli import app
from autosentry.config import DEFAULT_CONFIG_PATH, load_config
from autosentry.diagnosis import build_bundle, render_bundle


@app.command()
def explain(
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),  # noqa: B008
    run_id: str | None = typer.Option(None, "--run-id"),
    as_json: bool = typer.Option(False, "--json"),
    output: Path | None = typer.Option(None, "--output", "-o"),  # noqa: B008
) -> None:
    """Explain a run using a cited evidence bundle. Makes no provider calls."""
    try:
        bundle = build_bundle(load_config(config), run_id)
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    text = json.dumps(bundle, indent=2) + "\n" if as_json else render_bundle(bundle)
    if output:
        output.write_text(text, encoding="utf-8")
    else:
        typer.echo(text, nl=False)
