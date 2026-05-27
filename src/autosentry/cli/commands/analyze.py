"""``autosentry analyze`` — attempts ledger summary."""

from __future__ import annotations

import json as _json
from pathlib import Path

import typer
from rich.table import Table

from autosentry.cli import app
from autosentry.cli.style import ACCENT, ERR, INFO, console
from autosentry.config import load_config


@app.command()
def analyze(
    config: Path = typer.Option(  # noqa: B008
        Path("autosentry.yaml"),
        "--config",
        "-c",
        help="Path to autosentry.yaml. Defaults to ./autosentry.yaml.",
    ),
    since: str = typer.Option(
        "",
        "--since",
        help=(
            "Filter window — e.g. 24h, 30m, 7d, 600s. Empty (the default) = "
            "all-time. Useful before deciding to codify a new rule."
        ),
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Print as JSON instead of rich tables. Pipe to jq for scripting.",
    ),
) -> None:
    """Summarize the attempts ledger (`.autosentry/attempts.tsv`).

    Three sections: top-failing detectors (sorted by attempt count
    inside the window), per-rule success rate (kept vs regressed vs
    crashed vs pending), and a regression-streak callout when a
    detector has been failing repeatedly.

    Use this to spot patterns worth codifying as YAML rules: when
    three+ Claude-driven fixes for the same detector all `kept`,
    that's a strong signal the pattern deserves a deterministic
    rule.
    """
    from autosentry.analyze import filter_window, parse_since, summarize
    from autosentry.ledger import AttemptsLedger

    cfg = load_config(config)
    ledger_path = cfg.resolve(".autosentry/attempts.tsv")
    ledger = AttemptsLedger.load(ledger_path)
    if not ledger.rows:
        console.print(f"[dim]no attempts yet ({ledger_path})[/dim]")
        return

    try:
        window = parse_since(since)
    except ValueError as e:
        console.print(f"[{ERR}]{e}[/{ERR}]")
        raise typer.Exit(code=2) from e

    rows = filter_window(ledger.rows, window)
    summary = summarize(rows)

    if json_out:
        typer.echo(_json.dumps(_summary_to_dict(summary), indent=2))
        return

    title = f"attempts — {summary.total} total"
    if window is not None:
        title += f" (last {since})"
    console.print(f"\n[bold]{title}[/bold]")
    status_str = "  ".join(
        f"[{INFO}]{k}[/{INFO}]={v}" for k, v in sorted(summary.by_status.items())
    )
    console.print(status_str + "\n")

    if summary.top_detectors:
        t = Table(title="top failing detectors", show_header=True, header_style="bold")
        t.add_column("detector")
        t.add_column("attempts", justify="right")
        for name, count in summary.top_detectors:
            t.add_row(name, str(count))
        console.print(t)

    if summary.rules:
        t = Table(title="per-rule success", show_header=True, header_style="bold")
        t.add_column("source (rule or claude)")
        t.add_column("total", justify="right")
        t.add_column("kept", justify="right")
        t.add_column("regressed", justify="right")
        t.add_column("crashed", justify="right")
        t.add_column("pending", justify="right")
        t.add_column("success", justify="right")
        for r in summary.rules:
            t.add_row(
                r.source,
                str(r.total),
                str(r.kept),
                str(r.regressed),
                str(r.crashed),
                str(r.pending),
                f"{r.success_rate * 100:.0f}%",
            )
        console.print(t)

    streaks_with_regression = [
        s for s in summary.detector_streaks if s.streak_status == "regressed" and s.streak_len >= 2
    ]
    if streaks_with_regression:
        t = Table(
            title=f"[{ACCENT}]regression streaks (heads up)[/{ACCENT}]",
            show_header=True,
            header_style="bold",
        )
        t.add_column("detector")
        t.add_column("streak", justify="right")
        for s in streaks_with_regression:
            t.add_row(s.detector, f"{s.streak_len}× regressed")
        console.print(t)


def _summary_to_dict(summary) -> dict:  # noqa: ANN001 — Summary is internal
    return {
        "total": summary.total,
        "by_status": summary.by_status,
        "top_detectors": summary.top_detectors,
        "rules": [
            {
                "source": r.source,
                "total": r.total,
                "kept": r.kept,
                "regressed": r.regressed,
                "crashed": r.crashed,
                "discarded": r.discarded,
                "pending": r.pending,
                "success_rate": r.success_rate,
            }
            for r in summary.rules
        ],
        "detector_streaks": [
            {
                "detector": s.detector,
                "total": s.total,
                "streak_status": s.streak_status,
                "streak_len": s.streak_len,
            }
            for s in summary.detector_streaks
        ],
    }
