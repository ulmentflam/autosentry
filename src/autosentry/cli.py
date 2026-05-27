"""autosentry CLI.

Subcommands:

- ``init`` — write a default ``autosentry.yaml`` and ``.autosentry/`` into the cwd.
- ``run`` — start the monitor against an existing config.
- ``status`` — print a short summary of the current state.
- ``incidents list|show`` — browse the incident store.
- ``dispatcher`` — drain the slack outbox (skeleton; meant to be invoked by a
  separate Claude session with MCP Slack tools).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from autosentry import __version__
from autosentry.config import load_config
from autosentry.monitor import Monitor
from autosentry.state import StateStore

app = typer.Typer(
    name="autosentry",
    no_args_is_help=True,
    add_completion=False,
    help="Self-healing sentry for long-running processes.",
)
incidents_app = typer.Typer(no_args_is_help=True, help="Browse incident folders.")
app.add_typer(incidents_app, name="incidents")
console = Console()

TEMPLATES = Path(__file__).parent / "templates"


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V", help="Print version and exit.", is_flag=True
    ),
) -> None:
    if version:
        typer.echo(f"autosentry {__version__}")
        raise typer.Exit()


@app.command()
def init(
    target: Path = typer.Argument(Path("."), help="Directory to initialize (default: cwd)"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing autosentry.yaml"),
) -> None:
    """Drop autosentry.yaml + .autosentry/ skeleton into a repo."""
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)

    yaml_dst = target / "autosentry.yaml"
    yaml_src = TEMPLATES / "autosentry.yaml.tmpl"
    if yaml_dst.exists() and not force:
        console.print(f"[yellow]autosentry.yaml already exists at {yaml_dst}[/yellow]")
        console.print("Pass --force to overwrite.")
        raise typer.Exit(code=1)
    shutil.copy(yaml_src, yaml_dst)

    (target / ".autosentry").mkdir(exist_ok=True)
    (target / ".autosentry" / "incidents").mkdir(exist_ok=True)
    (target / ".autosentry" / "logs").mkdir(exist_ok=True)
    prompts = target / ".autosentry" / "prompts"
    prompts.mkdir(exist_ok=True)
    shutil.copy(TEMPLATES / "recovery.md.tmpl", prompts / "recovery.md")

    console.print(f"[green]initialized autosentry at[/green] {target}")
    console.print(f"  config: {yaml_dst}")
    console.print(f"  state:  {target}/.autosentry/state.json")
    console.print("  next:   edit autosentry.yaml, then `autosentry run`")


@app.command()
def run(
    config: Path = typer.Option(
        Path("autosentry.yaml"), "--config", "-c", help="Path to autosentry.yaml"
    ),
) -> None:
    """Start the monitor."""
    cfg = load_config(config)
    Monitor(cfg).run()


@app.command()
def status(
    config: Path = typer.Option(Path("autosentry.yaml"), "--config", "-c"),
) -> None:
    """Print a short status summary from state.json."""
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


@incidents_app.command("list")
def incidents_list(
    config: Path = typer.Option(Path("autosentry.yaml"), "--config", "-c"),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
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
    incident_id: str,
    config: Path = typer.Option(Path("autosentry.yaml"), "--config", "-c"),
) -> None:
    cfg = load_config(config)
    folder = cfg.resolve(cfg.incidents_dir) / incident_id
    report = folder / "report.md"
    if not report.exists():
        console.print(f"[red]incident not found:[/red] {folder}")
        raise typer.Exit(code=1)
    console.print(report.read_text(encoding="utf-8"))


@app.command()
def dispatcher(
    config: Path = typer.Option(Path("autosentry.yaml"), "--config", "-c"),
    once: bool = typer.Option(False, "--once", help="Drain pending and exit"),
) -> None:
    """Drain the Slack outbox.

    The dispatcher is intentionally a skeleton — it prints pending entries
    and marks them sent. In practice you point a Claude session with MCP
    Slack tools at this command (and have it send each entry's body before
    marking sent). Kept here so the file-outbox format has a reference
    consumer in this repo.
    """
    cfg = load_config(config)
    outboxes = [n for n in cfg.notifiers if n.kind == "slack_outbox"]
    if not outboxes:
        console.print("[yellow]no slack_outbox notifier configured[/yellow]")
        raise typer.Exit(code=1)
    outbox_path = cfg.resolve(outboxes[0].outbox_path)
    if not outbox_path.exists():
        console.print("[dim]outbox empty[/dim]")
        return

    entries: list[dict] = []
    with outbox_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    pending = [e for e in entries if not e.get("sent")]
    console.print(f"[bold]{len(pending)} pending[/bold] of {len(entries)} total")
    for e in pending:
        body = (e.get("body") or "")[:100]
        console.print(f"  → [{e.get('kind')}] {e.get('title')}: {body}")

    if once:
        return
    console.print("[dim]Run via a Claude session with MCP Slack to actually deliver these.[/dim]")


if __name__ == "__main__":
    app()
