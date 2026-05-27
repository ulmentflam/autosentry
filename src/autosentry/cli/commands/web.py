"""``autosentry web`` — read-only HTTP incident viewer."""

from __future__ import annotations

from pathlib import Path

import typer

from autosentry.cli import app
from autosentry.config import load_config


@app.command()
def web(
    config: Path = typer.Option(  # noqa: B008
        Path("autosentry.yaml"),
        "--config",
        "-c",
        help="Path to autosentry.yaml. Defaults to ./autosentry.yaml.",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind address. Default localhost-only; use 0.0.0.0 to expose on the LAN.",
    ),
    port: int = typer.Option(8765, "--port", help="Listen port (default 8765)."),
) -> None:
    """Serve the incident folders over a small read-only HTTP UI.

    Three routes: an index of incidents from `index.jsonl` with a
    client-side filter, a detail page that renders the report.md as
    HTML, and per-artifact endpoints (`trace.txt`, frame files, config
    snapshots, fix diff). Stdlib `http.server` only — no Flask/FastAPI.

    Path-traversal defended. Binds localhost by default; print a
    warning when you flip --host to 0.0.0.0 since incident contents
    include log excerpts and config snapshots.
    """
    from autosentry.web import run_server

    cfg = load_config(config)
    run_server(cfg.resolve(cfg.incidents_dir), host=host, port=port)
