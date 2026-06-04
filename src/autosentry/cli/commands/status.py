"""``autosentry status`` — one-shot state.json summary."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from autosentry.cli import app
from autosentry.cli.style import console
from autosentry.config import DEFAULT_CONFIG_PATH, load_config
from autosentry.pipeline import load_pipeline_state
from autosentry.state import StateStore


@app.command()
def status(
    config: Path = typer.Option(  # noqa: B008
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help="Path to the config. Defaults to .autosentry/autosentry.yaml "
        "(falls back to ./autosentry.yaml).",
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

    # Pipeline view (only shown when stages are configured). Renders first
    # so the operator sees overall progress before stage-internal counters.
    if cfg.process.is_pipeline():
        pipeline = load_pipeline_state(cfg.resolve(Path(".autosentry/pipeline.json")))
        pt = Table(
            title=f"pipeline · {pipeline.status if pipeline else 'not yet run'}",
            show_header=True,
            header_style="bold",
        )
        pt.add_column("#")
        pt.add_column("stage")
        pt.add_column("status")
        pt.add_column("exit")
        pt.add_column("restarts")
        results = {r.name: r for r in pipeline.stages} if pipeline else {}
        for idx, stage in enumerate(cfg.process.stages, start=1):
            r = results.get(stage.name)
            pt.add_row(
                str(idx),
                stage.name + (" ←" if pipeline and pipeline.current_stage == stage.name else ""),
                r.status if r else "pending",
                str(r.exit_code) if r and r.exit_code is not None else "-",
                str(r.final_restarts) if r else "-",
            )
        console.print(pt)

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

    if state.reset_history:
        recent_resets = state.reset_history[-5:]
        zt = Table(title="recent resets", show_header=True, header_style="bold")
        zt.add_column("time")
        zt.add_column("source")
        zt.add_column("cleared (restarts → 0 from)")
        zt.add_column("reason")
        for r in recent_resets:
            cleared = f"{r.prior_restarts}"
            if r.cleared_history:
                cleared += " + history"
            if r.stage:
                cleared = f"stage={r.stage} · {cleared}"
            zt.add_row(r.time, r.source, cleared, (r.reason or "")[:80])
        console.print(zt)
