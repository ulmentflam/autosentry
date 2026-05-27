"""``autosentry status`` — one-shot state.json summary."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from autosentry.cli import app
from autosentry.cli.style import console
from autosentry.config import load_config
from autosentry.state import StateStore


@app.command()
def status(
    config: Path = typer.Option(  # noqa: B008
        Path("autosentry.yaml"),
        "--config",
        "-c",
        help="Path to autosentry.yaml. Defaults to ./autosentry.yaml.",
    ),
) -> None:
    """One-shot snapshot of the monitor's persisted state.

    Reads `.autosentry/state.json` and prints pid, started_at, last
    heartbeat, last exit code, restarts (unverified vs cap), and the
    count of recently observed anomalies. For a live continuously-
    refreshing view, use `autosentry watch` instead.
    """
    cfg = load_config(config)
    store = StateStore(cfg.resolve(cfg.state_path))
    state = store.load()

    table = Table(title="autosentry status", show_header=True, header_style="bold")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("pid", str(state.pid))
    table.add_row("started_at", str(state.started_at))
    table.add_row("last_heartbeat", str(state.last_heartbeat))
    table.add_row("last_exit_code", str(state.last_exit_code))
    table.add_row("restarts", f"{state.restarts} / {state.max_restarts}")
    table.add_row("anomalies (recent)", str(len(state.anomalies)))
    console.print(table)

    if state.restart_history:
        recent = state.restart_history[-5:]
        rt = Table(title="recent restarts", show_header=True, header_style="bold")
        rt.add_column("time")
        rt.add_column("rule")
        rt.add_column("reason")
        for r in recent:
            rt.add_row(r.time, r.rule or "-", (r.reason or "")[:80])
        console.print(rt)
