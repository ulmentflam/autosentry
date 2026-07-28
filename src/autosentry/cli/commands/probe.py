"""``autosentry probe`` — one-shot liveness + pending-incidents probe.

Used by the interactive Claude Code session's Stop hook (and by anyone
who wants a scriptable view of "is autosentry healthy, and is there work
for the session to dispatch?"). Returns structured JSON on stdout so a
hook can act on it programmatically.

Output shape (stable; bump ``schema_version`` on breaking changes)::

    {
      "schema_version": 2,
      "config_path": "...",
      "monitor": {
        "running": true,            # PID alive AND heartbeat fresh
        "pid": 12345,
        "pid_alive": true,
        "last_heartbeat": "2026-05-30T18:15:19+00:00",
        "heartbeat_age_seconds": 12.4,
        "stale": false,             # heartbeat older than the stall threshold
        "last_exit_code": null,
        "child_running": true,      # is there actually a child under it?
        "child_dead_seconds": null, # how long there hasn't been one
        "wedged": false,            # heartbeating fine, supervising nothing
        "stage": "pretrain",        # pipeline position, null off-pipeline
        "stage_index": 1,
        "stage_count": 3
      },
      "dispatch_mode": "session",
      "pending_incidents": [
        {
          "id": "2026-05-30T18-14-22Z-error-traceback",
          "detector": "traceback",
          "kind": "error",
          "message": "ValueError: boom",
          "rule_match": "oom" | null,
          "suggested_action": {"kind": "restart", ...} | null,
          "folder": ".autosentry/incidents/.../"
        }
      ],
      "summary": "1 pending incident; monitor healthy"
    }

The ``--inject-prompt`` flag also emits a Claude Code Stop-hook JSON
payload to stdout that re-engages the model on the next turn when there's
pending work or when the monitor needs reviving.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from autosentry.cli import app
from autosentry.config import DEFAULT_CONFIG_PATH, AutoSentryConfig, load_config

#: Bumped on breaking schema changes to the probe JSON output.
#: v2 added the child-liveness / wedge fields under ``monitor``.
PROBE_SCHEMA_VERSION = 2


def _pid_alive(pid: int | None) -> bool:
    """Best-effort PID liveness check. ``os.kill(pid, 0)`` is the standard
    Unix idiom — sends no signal but raises if the pid isn't ours to signal
    (i.e. dead, or owned by another user)."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _read_state(cfg: AutoSentryConfig) -> dict[str, Any]:
    state_path = cfg.resolve(cfg.state_path)
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_cursor(cfg: AutoSentryConfig) -> str | None:
    cursor = cfg.resolve(cfg.dispatch.cursor_path)
    if not cursor.exists():
        return None
    return cursor.read_text(encoding="utf-8").strip() or None


def _write_cursor(cfg: AutoSentryConfig, incident_id: str) -> None:
    cursor = cfg.resolve(cfg.dispatch.cursor_path)
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(incident_id, encoding="utf-8")


def _pending_incidents(cfg: AutoSentryConfig, after: str | None) -> list[dict[str, Any]]:
    """Return index entries newer than ``after`` (exclusive). ``after`` is
    an incident id; we compare lexicographically because the id starts with
    an ISO-8601 timestamp."""
    index = cfg.resolve(cfg.incidents_dir) / "index.jsonl"
    if not index.exists():
        return []
    out: list[dict[str, Any]] = []
    with index.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if after is not None and entry.get("id", "") <= after:
                continue
            out.append(entry)
    return out


def _match_rule(cfg: AutoSentryConfig, detector: str, message: str) -> tuple[str | None, Any]:
    """First-rule-wins match against the configured rules. Returns
    ``(rule_name, action_dict)`` or ``(None, None)``."""
    for r in cfg.rules:
        if r.match.detector != detector:
            continue
        if r.match.message_regex and not re.search(r.match.message_regex, message):
            continue
        name = r.name or r.match.detector
        return name, r.action.model_dump()
    return None, None


def _build_payload(cfg: AutoSentryConfig) -> dict[str, Any]:
    state = _read_state(cfg)
    pid = state.get("pid")
    last_hb = state.get("last_heartbeat")
    hb_dt = _parse_iso(last_hb)
    hb_age = (datetime.now(tz=timezone.utc) - hb_dt).total_seconds() if hb_dt is not None else None
    stall_threshold = float(cfg.monitor.stall_timeout_seconds)
    # ``stale`` is the operational "monitor looks alive but isn't ticking"
    # signal — useful when ``pid_alive`` is True but the loop is stuck.
    stale = hb_age is not None and hb_age > stall_threshold
    pid_alive = _pid_alive(pid)
    running = pid_alive and (hb_age is None or hb_age <= stall_threshold)

    # "Wedged" is the failure mode a liveness check can't see: the
    # supervisor process is up and the heartbeat is fresh, but there is no
    # child underneath it and hasn't been for a long time (issue #22).
    # ``pid_alive and not stale`` is exactly what every naive check calls
    # healthy, so that's the case worth naming.
    child_running = bool(state.get("child_running"))
    child_dead_dt = _parse_iso(state.get("child_dead_since"))
    child_dead_seconds = (
        (datetime.now(tz=timezone.utc) - child_dead_dt).total_seconds()
        if child_dead_dt is not None
        else None
    )
    # The monitor's own backstop threshold when set; otherwise fall back to
    # the stall threshold so the probe still reports something useful for
    # users who disabled the backstop.
    wedge_threshold = float(cfg.monitor.dead_child_grace_seconds or stall_threshold)
    wedged = (
        pid_alive
        and not stale
        and not child_running
        and child_dead_seconds is not None
        and child_dead_seconds > wedge_threshold
    )

    cursor = _read_cursor(cfg)
    entries = _pending_incidents(cfg, after=cursor)
    pending = []
    for e in entries:
        rule_name, action = _match_rule(cfg, e.get("detector", ""), e.get("message", ""))
        folder = cfg.resolve(cfg.incidents_dir) / e["id"]
        pending.append(
            {
                "id": e["id"],
                "detector": e.get("detector"),
                "kind": e.get("kind"),
                "message": e.get("message"),
                "rule_match": rule_name,
                "suggested_action": action,
                "folder": str(folder),
            }
        )

    stage = state.get("stage")
    stage_note = f" [stage {stage}]" if stage else ""

    if not pid_alive:
        summary = f"monitor DOWN (pid={pid}); {len(pending)} pending incident(s)"
    elif stale:
        summary = (
            f"monitor STALE (heartbeat {hb_age:.0f}s old, threshold {stall_threshold:.0f}s); "
            f"{len(pending)} pending incident(s)"
        )
    elif wedged:
        summary = (
            f"monitor WEDGED{stage_note} — heartbeat fresh but no child for "
            f"{child_dead_seconds:.0f}s (threshold {wedge_threshold:.0f}s); "
            f"{len(pending)} pending incident(s)"
        )
    elif pending:
        summary = f"monitor healthy; {len(pending)} pending incident(s)"
    else:
        summary = "monitor healthy; no pending incidents"

    return {
        "schema_version": PROBE_SCHEMA_VERSION,
        "config_path": str(cfg.config_path) if cfg.config_path else None,
        "monitor": {
            "running": running,
            "pid": pid,
            "pid_alive": pid_alive,
            "last_heartbeat": last_hb,
            "heartbeat_age_seconds": hb_age,
            "stale": stale,
            "last_exit_code": state.get("last_exit_code"),
            "child_running": child_running,
            "child_dead_seconds": child_dead_seconds,
            "wedged": wedged,
            "stage": stage,
            "stage_index": state.get("stage_index"),
            "stage_count": state.get("stage_count"),
        },
        "dispatch_mode": cfg.dispatch.mode,
        "pending_incidents": pending,
        "summary": summary,
    }


def _inject_prompt(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return a Claude Code Stop-hook JSON payload that re-engages the
    model when there's autosentry work to do. ``None`` if nothing needs
    the session's attention.

    Stop hooks read this JSON from the hook's stdout and use
    ``decision: "block"`` to prevent the agent from concluding the turn
    and re-prompt with ``reason`` as additional instruction.
    """
    monitor = payload["monitor"]
    pending = payload["pending_incidents"]
    # ``wedged`` arrived in schema v2; read it leniently so a v1 payload
    # (or a hand-built one) still routes through the other branches.
    wedged = bool(monitor.get("wedged"))
    if not pending and monitor["pid_alive"] and not monitor["stale"] and not wedged:
        return None

    lines = ["autosentry: session-watch fired."]
    if not monitor["pid_alive"]:
        lines.append(
            f"  Monitor is DOWN (pid={monitor['pid']}). Restart it with "
            f"`nohup autosentry run > /dev/null 2>&1 &` before processing incidents."
        )
    elif monitor["stale"]:
        age = monitor["heartbeat_age_seconds"]
        lines.append(
            f"  Monitor heartbeat is STALE ({age:.0f}s old). "
            f"It may be hung — consider restarting it."
        )
    elif wedged:
        dead = monitor.get("child_dead_seconds") or 0.0
        stage = f" on stage {monitor.get('stage')}" if monitor.get("stage") else ""
        lines.append(
            f"  Monitor is WEDGED{stage}: heartbeat is fresh but it has had no "
            f"child for {dead:.0f}s. It is supervising nothing — restart it."
        )
    if pending:
        lines.append(f"  {len(pending)} pending incident(s) waiting for session dispatch:")
        for inc in pending[:5]:
            rule = inc.get("rule_match") or "no-rule"
            lines.append(
                f"    - {inc['id']} [{inc['detector']}] rule={rule} — {inc['message'][:120]}"
            )
        if len(pending) > 5:
            lines.append(f"    … and {len(pending) - 5} more")
        lines.append(
            "  Invoke the /autosentry skill: read each incident folder, match it to the "
            "rule (or escalate via Task tool when no rule matches), and advance the "
            "cursor via `autosentry probe --advance-cursor <last-id>` when done."
        )
    return {
        "decision": "block",
        "reason": "\n".join(lines),
    }


@app.command()
def probe(
    config: Path = typer.Option(  # noqa: B008
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help="Path to the config (default .autosentry/autosentry.yaml).",
    ),
    inject_prompt: bool = typer.Option(
        False,
        "--inject-prompt",
        help="Emit a Claude Code Stop-hook JSON payload to stdout when there's "
        "autosentry work for the session to handle. Without this flag, "
        "stdout is the structured probe JSON.",
    ),
    advance_cursor: str | None = typer.Option(
        None,
        "--advance-cursor",
        help="Write the given incident id to the session-dispatch cursor "
        "so subsequent probes don't re-report it. Used by the /autosentry "
        "skill after it finishes handling an incident.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress the human-readable summary on stderr.",
    ),
) -> None:
    """Probe autosentry liveness + pending incidents (one-shot).

    The interactive Claude Code session's Stop hook runs this after every
    assistant turn; the JSON output (or ``--inject-prompt`` payload) tells
    the session whether to wake up and dispatch healers.

    Exit codes:
      0 — monitor healthy, no pending work
      1 — pending incidents exist OR monitor is down / stale / wedged
    """
    cfg = load_config(config)

    if advance_cursor is not None:
        _write_cursor(cfg, advance_cursor)
        if not quiet:
            sys.stderr.write(f"cursor advanced to {advance_cursor}\n")

    payload = _build_payload(cfg)

    if inject_prompt:
        injected = _inject_prompt(payload)
        if injected is not None:
            sys.stdout.write(json.dumps(injected))
            sys.stdout.write("\n")
        # No work → empty stdout so the Stop hook lets the turn conclude.
    else:
        sys.stdout.write(json.dumps(payload, indent=2))
        sys.stdout.write("\n")

    if not quiet:
        sys.stderr.write(payload["summary"] + "\n")

    monitor_info = payload["monitor"]
    has_work = (
        bool(payload["pending_incidents"])
        or not monitor_info["pid_alive"]
        or monitor_info["stale"]
        or monitor_info.get("wedged", False)
    )
    raise typer.Exit(code=1 if has_work else 0)
