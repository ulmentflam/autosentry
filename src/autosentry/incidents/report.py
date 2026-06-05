"""Render a Markdown ``report.md`` for an incident folder."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from autosentry.state import format_budget

if TYPE_CHECKING:
    from autosentry.incidents.store import IncidentWrite


def render_report(w: IncidentWrite, *, folder: Path) -> str:
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    parts: list[str] = []
    parts.append(f"# Incident — {ts} — {w.kind} / {w.detector}\n")
    parts.append(
        f"**Process:** {w.process_kind} · `{' '.join(w.command)}`  \n"
        f"**PID:** {w.pid}  **Restart #:** {w.restart_index}/{format_budget(w.max_restarts)}  \n"
        f"**Detector:** `{w.detector}` ({w.kind})  \n"
    )
    if w.rule_match:
        rule = w.rule_match.get("rule") or "<unnamed>"
        action_kind = (w.action or {}).get("kind", "?") if w.action else "?"
        parts.append(f"**Resolution:** rule `{rule}` → {action_kind}\n")
    elif w.claude_response:
        parts.append("**Resolution:** Claude (no rule matched)\n")
    else:
        parts.append("**Resolution:** *(none — no rule matched and Claude disabled)*\n")
    parts.append("\n---\n")

    # message / summary
    parts.append(f"\n## Summary\n\n{w.message}\n")

    # Source frames
    if w.frames:
        parts.append("\n## Source frames\n")
        for i, f in enumerate(w.frames, start=1):
            parts.append(f"\n### Frame {i} — `{f.file}:{f.line}` ({f.lang})\n")
            if f.skipped:
                parts.append(f"\n*Skipped: {f.skipped}*\n")
                parts.append(f"\nRaw: `{f.raw.strip()}`\n")
                continue
            if f.enclosing:
                parts.append(f"\n**Enclosing:** `{f.enclosing}`\n")
            parts.append(f"\n```{f.lang}\n{f.source or ''}\n```\n")

    # Stack trace
    if w.trace:
        parts.append("\n## Stack trace\n")
        parts.append(f"\n```\n{w.trace}\n```\n")

    # Configs at incident
    if w.config_snapshot_paths:
        parts.append("\n## Configs at incident\n")
        for src in w.config_snapshot_paths:
            snap = folder / "configs" / src.name
            parts.append(f"- `{src}` — snapshot: `{snap.relative_to(folder)}`\n")

    # State (compact)
    parts.append("\n## State at incident\n")
    parts.append("\nSee `state.json`.\n")

    # Log excerpt link (kept out of the report itself to avoid huge files)
    parts.append("\n## Log excerpt\n")
    parts.append("\nSee `log_excerpt.txt` for the lines leading up to this incident.\n")

    # Fix
    parts.append("\n## Fix applied\n")
    if w.rule_match and w.action:
        env_set = w.action.get("set") or {}
        rule = w.rule_match.get("rule") or "<unnamed>"
        if w.action.get("kind") == "restart_with_env" and env_set:
            envs = ", ".join(f"`{k}={v}`" for k, v in env_set.items())
            parts.append(f"\nRule `{rule}` matched. Restarted with overrides: {envs}.\n")
        else:
            parts.append(f"\nRule `{rule}` matched. Applied action `{w.action.get('kind')}`.\n")
    elif w.claude_response:
        parts.append("\nClaude was invoked. See `fix/claude_response.md`.\n")
    else:
        parts.append("\n*(no fix applied)*\n")
    if w.claude_diff:
        parts.append("\nFile edits captured in `fix/diff.patch`.\n")

    return "".join(parts)
