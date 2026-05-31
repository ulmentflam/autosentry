"""``autosentry web`` — read-only HTTP incident viewer."""

from __future__ import annotations

from pathlib import Path

import typer

from autosentry.cli import app
from autosentry.config import DEFAULT_CONFIG_PATH, load_config


@app.command()
def web(
    config: Path = typer.Option(  # noqa: B008
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help="Path to the config. Defaults to .autosentry/autosentry.yaml "
        "(falls back to ./autosentry.yaml).",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind address. Default localhost-only; use 0.0.0.0 to expose on the LAN.",
    ),
    port: int = typer.Option(8765, "--port", help="Listen port (default 8765)."),
) -> None:
    """Serve the incident folders + vault over a small read-only HTTP UI.

    Routes: an index of incidents from `index.jsonl` with a client-side
    filter, a detail page that renders each `report.md` as HTML,
    per-artifact endpoints (`trace.txt`, frame files, config snapshots,
    fix diff), and (0.12.0) a vault browser at ``/vault`` + a Mermaid
    graph view at ``/vault/graph``. Stdlib `http.server` only — no
    Flask/FastAPI.

    Path-traversal defended. Binds localhost by default; print a
    warning when you flip --host to 0.0.0.0 since incident contents
    include log excerpts and config snapshots.
    """
    from autosentry.web import run_server

    cfg = load_config(config)
    vault_root = cfg.resolve(cfg.vault.path) if cfg.vault.enabled else None
    run_server(
        cfg.resolve(cfg.incidents_dir),
        host=host,
        port=port,
        vault_root=vault_root,
    )
