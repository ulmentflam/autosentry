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

    Routes:

    ``GET /``
        Redirects to ``/incidents``.
    ``GET /incidents``
        Index of all incidents (newest first) with a client-side filter.
        Sourced from ``incidents/index.jsonl``.
    ``GET /incidents/<id>``
        Full incident report (``report.md`` rendered as HTML), exploded
        source frames, snapshotted configs, and the fix diff.
    ``GET /incidents/<id>/raw/<file>``
        Raw artifact within the incident folder — ``trace.txt``,
        frame files (``frames/``), config snapshots (``configs/``),
        ``state.json``, ``rule_match.json``, fix files (``fix/``).
    ``GET /vault``
        Categorized index of vault notes: runs, incidents, detectors,
        patterns, regressions, exhaustions. Only available when
        ``vault.enabled: true`` (the default).
    ``GET /vault/<subdir>/<file>``
        Render a vault note. Obsidian ``[[wikilinks]]`` are resolved to
        in-app URLs. Nested notes (e.g. attempt notes under an incident)
        are addressed via the wikilink-id convention
        ``<parent>-<child>`` mapping to ``<subdir>/<parent>/<child>.md``.
    ``GET /vault/graph``
        Mermaid ``graph TD`` of run → child-restart → incident →
        attempt → outcome chains; pattern aggregators shown as dotted
        edges. Click any node to drill into its vault note.
    ``GET /healthz``
        Liveness probe; returns ``200 ok``.

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
