"""``autosentry incidents`` subcommands."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.table import Table

from autosentry.cli import app
from autosentry.cli.style import ERR, console
from autosentry.config import DEFAULT_CONFIG_PATH, load_config

incidents_app = typer.Typer(no_args_is_help=True, help="Browse incident folders.")
app.add_typer(incidents_app, name="incidents")


@incidents_app.command("list")
def incidents_list(
    config: Path = typer.Option(  # noqa: B008
        DEFAULT_CONFIG_PATH, "--config", "-c", help="Path to the config."
    ),
    limit: int = typer.Option(
        20, "--limit", "-n", help="Show at most this many recent incidents (default 20)."
    ),
) -> None:
    """List the most recent incidents from the incident store.

    Reads `.autosentry/incidents/index.jsonl` and prints a one-line
    summary per incident: id, kind, detector that fired, rule (or
    `claude`), action applied, message. Use `autosentry incidents
    show <id>` for the full report.md.
    """
    cfg = load_config(config)
    idx = cfg.resolve(cfg.incidents_dir) / "index.jsonl"
    if not idx.exists():
        console.print("[dim]no incidents yet[/dim]")
        return
    rows: list[dict] = []
    with idx.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows = rows[-limit:]
    t = Table(title=f"last {len(rows)} incidents", show_header=True, header_style="bold")
    t.add_column("id")
    t.add_column("kind")
    t.add_column("detector")
    t.add_column("rule")
    t.add_column("action")
    t.add_column("message")
    for r in rows:
        t.add_row(
            r.get("id", "?"),
            r.get("kind", "?"),
            r.get("detector", "?"),
            str(r.get("rule") or "-"),
            str(r.get("action") or "-"),
            (r.get("message") or "")[:80],
        )
    console.print(t)


@incidents_app.command("show")
def incidents_show(
    incident_id: str = typer.Argument(  # noqa: B008
        ..., help="Incident id (the folder name under .autosentry/incidents/)."
    ),
    config: Path = typer.Option(  # noqa: B008
        DEFAULT_CONFIG_PATH, "--config", "-c", help="Path to the config."
    ),
) -> None:
    """Print the rendered report.md for a single incident.

    The full report includes exploded stack frames around the failure
    site, snapshots of every declared config file, the rule that fired
    (or Claude's diagnosis + diff), and the action that was applied.
    The raw artifacts (`trace.txt`, `frames/`, `configs/`, `fix/`,
    `state.json`) live alongside the report in the incident folder.
    """
    cfg = load_config(config)
    folder = cfg.resolve(cfg.incidents_dir) / incident_id
    report = folder / "report.md"
    if not report.exists():
        console.print(f"[{ERR}]incident not found:[/{ERR}] {folder}")
        raise typer.Exit(code=1)
    console.print(report.read_text(encoding="utf-8"))
