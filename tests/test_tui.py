"""TUI renderer tests.

We don't drive a real terminal — we just check that ``gather_snapshot``
and ``render`` produce sensible output given a minimal state + incidents
index. The CLI itself is a thin shim around ``run_tui``.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from rich.console import Console

from autosentry.config import load_config
from autosentry.state import MonitorState, StateStore
from autosentry.tui import (
    _format_duration,
    _parse_iso,
    _relative_since,
    _style_for_log_line,
    gather_snapshot,
    render,
)


def _write_min_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "autosentry.yaml"
    cfg.write_text(
        dedent(
            """\
            process:
              kind: local
              command: ["true"]
            monitor:
              log_dir: ".autosentry/logs"
            detectors:
              - kind: pattern
                name: oom
                regex: "OOM"
              - kind: traceback
            state_path: ".autosentry/state.json"
            incidents_dir: ".autosentry/incidents"
            """
        )
    )
    return cfg


def test_gather_snapshot_with_empty_layout(tmp_path: Path):
    cfg_path = _write_min_config(tmp_path)
    cfg = load_config(cfg_path)
    snap = gather_snapshot(cfg)
    # No state file → all "—" sentinels, but the structure is intact.
    assert any(k == "pid" for k, _ in snap.state_summary)
    assert snap.incidents == []
    # Both detectors appear with "no hits".
    names = {n for n, _ in snap.detectors}
    assert names == {"oom", "traceback"}
    assert all("no hits" in v for _, v in snap.detectors)
    assert snap.log_tail == ["(no log file yet)"]


def test_gather_snapshot_reads_state_and_incidents(tmp_path: Path):
    cfg_path = _write_min_config(tmp_path)
    cfg = load_config(cfg_path)

    # Seed state.
    state = MonitorState(pid=4242, restarts=1, max_restarts=5)
    StateStore(cfg.resolve(cfg.state_path)).save(state)

    # Seed an incident index entry.
    idx = cfg.resolve(cfg.incidents_dir) / "index.jsonl"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(
        json.dumps(
            {
                "id": "2026-05-26T14-32-10Z-error-traceback",
                "kind": "error",
                "detector": "traceback",
                "rule": "oom_halve_batch",
                "action": "restart_with_env",
                "message": "OOM at step 42",
            }
        )
        + "\n"
    )

    # Seed a structured log line so the tail picks something up.
    log_path = cfg.resolve(cfg.monitor.log_dir) / "autosentry.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("[2026-05-26 14:32:10 UTC] [INFO] hello\n")

    snap = gather_snapshot(cfg)
    state_dict = dict(snap.state_summary)
    assert state_dict["pid"] == "4242"
    assert state_dict["restarts"] == "1 / 5"
    assert len(snap.incidents) == 1
    assert snap.incidents[0]["detector"] == "traceback"

    # Detector status for traceback should mention the incident id.
    traceback_row = next(s for n, s in snap.detectors if n == "traceback")
    assert "2026-05-26T14-32-10Z-error-traceback" in traceback_row
    assert any("hello" in line for line in snap.log_tail)


def test_render_returns_console_capturable(tmp_path: Path):
    cfg_path = _write_min_config(tmp_path)
    cfg = load_config(cfg_path)
    snap = gather_snapshot(cfg)
    console = Console(record=True, width=120)
    console.print(render(snap))
    out = console.export_text()
    assert "state" in out
    assert "recent incidents" in out
    assert "detectors" in out
    assert "log tail" in out


def test_format_duration():
    from datetime import timedelta

    assert _format_duration(timedelta(seconds=42)) == "42s"
    assert _format_duration(timedelta(seconds=120)) == "2m 0s"
    assert _format_duration(timedelta(seconds=3700)) == "1h 1m"
    assert _format_duration(timedelta(days=2, seconds=3700)) == "2d 1h"


def test_relative_since_with_none_returns_em_dash():
    assert _relative_since(None) == "—"


def test_parse_iso_handles_bad_input():
    assert _parse_iso("not-a-date") is None
    assert _parse_iso("") is None
    assert _parse_iso(None) is None
    assert _parse_iso("2026-01-01T00:00:00+00:00") is not None


def test_style_for_log_line_routes_by_level():
    assert _style_for_log_line("[2026] [ERROR] oh no") == "red"
    assert _style_for_log_line("[2026] [ANOMALY] hi") == "yellow"
    assert _style_for_log_line("[2026] [ACTION] hi") == "magenta"
    assert _style_for_log_line("[2026] [CLAUDE] hi") == "cyan"
    assert _style_for_log_line("[2026] [INFO] hi") == "white"
