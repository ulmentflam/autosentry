"""Status TUI — ``autosentry watch``.

A rich.Live dashboard for an operator tailing the monitor. Panels:

- **state**: pid, uptime, restarts, last heartbeat, last exit code
- **incidents**: the most recent N from ``incidents/index.jsonl``
- **detector status**: time since each declared detector last fired
- **log tail**: last N lines from ``.autosentry/logs/autosentry.log``

The TUI never writes to disk and never modifies state — it re-reads on
each tick. Refresh defaults to 1 second. Quit with ``q`` (or Ctrl-C).
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from autosentry.config import AutoSentryConfig
from autosentry.state import StateStore, format_budget

# ----- snapshot helpers ----------------------------------------------------


@dataclass
class TuiSnapshot:
    """Everything one render iteration needs, gathered from disk."""

    state_summary: list[tuple[str, str]]
    incidents: list[dict]
    detectors: list[tuple[str, str]]
    log_tail: list[str]


def gather_snapshot(
    cfg: AutoSentryConfig, *, incidents_limit: int = 8, log_tail_lines: int = 16
) -> TuiSnapshot:
    """Read state, incidents index, and the structured log."""
    store = StateStore(cfg.resolve(cfg.state_path))
    state = store.load()

    started = _parse_iso(state.started_at)
    uptime = _format_duration(_now() - started) if started else "—"
    heartbeat = _relative_since(state.last_heartbeat)

    state_summary: list[tuple[str, str]] = [
        ("pid", str(state.pid) if state.pid is not None else "—"),
        ("uptime", uptime),
        ("restarts", f"{state.restarts} / {format_budget(state.max_restarts)}"),
        ("last heartbeat", heartbeat),
        # Paired with the heartbeat on purpose: a fresh heartbeat over a
        # dead child is what a wedged supervisor looks like (issue #22).
        (
            "child",
            "running"
            if state.child_running
            else f"none for {_relative_since(state.child_dead_since)}",
        ),
        ("last exit code", str(state.last_exit_code) if state.last_exit_code is not None else "—"),
        ("recent anomalies", str(len(state.anomalies))),
    ]
    if state.stage:
        state_summary.insert(
            1, ("stage", f"{state.stage} ({state.stage_index}/{state.stage_count})")
        )

    incidents = _read_incidents_tail(cfg, incidents_limit)
    detectors = _detector_rows(cfg, incidents)
    log_tail = _tail_log_file(cfg.resolve(cfg.monitor.log_dir) / "autosentry.log", log_tail_lines)
    return TuiSnapshot(
        state_summary=state_summary,
        incidents=incidents,
        detectors=detectors,
        log_tail=log_tail,
    )


def _read_incidents_tail(cfg: AutoSentryConfig, n: int) -> list[dict]:
    idx = cfg.resolve(cfg.incidents_dir) / "index.jsonl"
    if not idx.exists():
        return []
    keep: deque[dict] = deque(maxlen=n)
    with idx.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                keep.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(keep)


def _detector_rows(cfg: AutoSentryConfig, incidents: Iterable[dict]) -> list[tuple[str, str]]:
    """For each configured detector, find when it last fired."""
    last_by_name: dict[str, str] = {}
    for inc in incidents:
        name = inc.get("detector")
        if name and name not in last_by_name:
            last_by_name[name] = inc.get("id", "")
    rows: list[tuple[str, str]] = []
    for d in cfg.detectors:
        # Detectors auto-name themselves to the kind when no explicit name is
        # set; mirror the same shape as the build_detectors logic so the row
        # label matches what appears in incidents.
        label = d.name or d.kind
        last = last_by_name.get(label)
        if last:
            rows.append((label, f"last fired in incident {last}"))
        else:
            rows.append((label, "no hits"))
    return rows


def _tail_log_file(path: Path, n: int) -> list[str]:
    """Read the last ``n`` lines of a file without slurping the whole thing."""
    if not path.exists():
        return ["(no log file yet)"]
    try:
        # The log file is small enough in practice; reading the tail with
        # readlines is simpler than a seek-from-end byte walk.
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-n:]]
    except OSError as e:
        return [f"(error reading log: {e})"]


# ----- rendering -----------------------------------------------------------


def render(snapshot: TuiSnapshot) -> RenderableType:
    state_table = Table.grid(padding=(0, 2))
    state_table.add_column(justify="right", style="dim")
    state_table.add_column()
    for k, v in snapshot.state_summary:
        state_table.add_row(k, v)

    incidents_table = Table(show_header=True, header_style="bold", expand=True, pad_edge=False)
    incidents_table.add_column("id", overflow="fold")
    incidents_table.add_column("kind")
    incidents_table.add_column("detector")
    incidents_table.add_column("rule")
    incidents_table.add_column("action")
    incidents_table.add_column("message", overflow="fold")
    if not snapshot.incidents:
        incidents_table.add_row("[dim]no incidents yet[/dim]", "", "", "", "", "")
    for inc in snapshot.incidents:
        incidents_table.add_row(
            inc.get("id", "?"),
            inc.get("kind", "?"),
            inc.get("detector", "?"),
            str(inc.get("rule") or "-"),
            str(inc.get("action") or "-"),
            (inc.get("message") or "")[:80],
        )

    detectors_table = Table.grid(padding=(0, 2))
    detectors_table.add_column(style="cyan")
    detectors_table.add_column()
    if not snapshot.detectors:
        detectors_table.add_row("[dim]no detectors configured[/dim]", "")
    for name, status in snapshot.detectors:
        detectors_table.add_row(name, status)

    log_text = Text()
    for line in snapshot.log_tail:
        log_text.append(line + "\n", style=_style_for_log_line(line))

    return Group(
        Panel(state_table, title="state", border_style="green"),
        Panel(incidents_table, title="recent incidents", border_style="yellow"),
        Panel(detectors_table, title="detectors", border_style="cyan"),
        Panel(log_text, title="log tail", border_style="blue"),
        Align.center(
            Text(
                f"refreshed at {_now().strftime('%H:%M:%S UTC')} — press Ctrl-C to quit",
                style="dim",
            )
        ),
    )


def _style_for_log_line(line: str) -> str:
    if "[ERROR]" in line:
        return "red"
    if "[ANOMALY]" in line:
        return "yellow"
    if "[RECOVERY]" in line or "[ACTION]" in line:
        return "magenta"
    if "[CLAUDE]" in line:
        return "cyan"
    if "[SLACK]" in line:
        return "blue"
    return "white"


# ----- main loop -----------------------------------------------------------


def run_tui(
    cfg: AutoSentryConfig,
    *,
    refresh: float = 1.0,
    console: Console | None = None,
    once: bool = False,
) -> None:
    """Block, refreshing the dashboard until Ctrl-C (or ``once=True``)."""
    refresh = max(0.25, float(refresh))
    console = console or Console()
    snapshot = gather_snapshot(cfg)
    if once:
        console.print(render(snapshot))
        return
    with Live(render(snapshot), console=console, refresh_per_second=1 / refresh) as live:
        try:
            while True:
                time.sleep(refresh)
                live.update(render(gather_snapshot(cfg)))
        except KeyboardInterrupt:
            pass


# ----- small utilities -----------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _relative_since(value: str | None) -> str:
    when = _parse_iso(value)
    if when is None:
        return "—"
    return _format_duration(_now() - when) + " ago"


def _format_duration(delta) -> str:
    """Render a timedelta as a compact h/m/s string."""
    total = int(delta.total_seconds())
    if total < 0:
        return "—"
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60}s"
    if total < 86400:
        return f"{total // 3600}h {(total % 3600) // 60}m"
    return f"{total // 86400}d {(total % 86400) // 3600}h"
