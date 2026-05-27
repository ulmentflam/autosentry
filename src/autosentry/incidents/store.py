"""Incident folder writer.

Each detection produces an :class:`IncidentWrite` — a self-contained
package the monitor hands to the store. The store materializes it as
a folder under ``incidents_dir`` and appends an index entry.

Folder layout::

    <ts>-<kind>/
      report.md
      trace.txt
      log_excerpt.txt
      frames/01-<file>.md, 02-…
      configs/<basename>          (copies of declared config_snapshots)
      state.json                  (monitor state at moment of incident)
      rule_match.json             (which rule fired)
      fix/
        action.json
        diff.patch                (if Claude edited files)
        claude_response.md        (if Claude was invoked)
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autosentry.incidents.exploder import ExplodedFrame
from autosentry.incidents.report import render_report
from autosentry.logger import log


@dataclass
class IncidentWrite:
    """Inputs to ``IncidentStore.write`` — everything the report needs."""

    kind: str  # 'error' | 'anomaly' | 'exit'
    detector: str
    message: str
    process_kind: str
    command: list[str]
    pid: int | None
    restart_index: int
    max_restarts: int
    log_excerpt: list[str]
    trace: str | None = None
    frames: list[ExplodedFrame] = field(default_factory=list)
    config_snapshot_paths: list[Path] = field(default_factory=list)
    state_snapshot: dict[str, Any] = field(default_factory=dict)
    rule_match: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    claude_response: str | None = None
    claude_diff: str | None = None


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(s: str, max_len: int = 60) -> str:
    cleaned = _SLUG_RE.sub("-", s).strip("-")
    return cleaned[:max_len] or "incident"


class IncidentStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.jsonl"

    def write(self, w: IncidentWrite) -> Path:
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        slug = _slug(f"{w.kind}-{w.detector}")
        folder = self.root / f"{ts}-{slug}"
        folder.mkdir(parents=True, exist_ok=False)
        anchor = self.root.parent.parent
        display = folder.relative_to(anchor) if folder.is_relative_to(anchor) else folder
        log().action(f"Incident folder: {display}")

        # raw trace
        if w.trace:
            (folder / "trace.txt").write_text(w.trace, encoding="utf-8")

        # log excerpt
        (folder / "log_excerpt.txt").write_text("\n".join(w.log_excerpt), encoding="utf-8")

        # exploded frames
        if w.frames:
            frames_dir = folder / "frames"
            frames_dir.mkdir()
            for i, f in enumerate(w.frames, start=1):
                fname = f"{i:02d}-{_slug(Path(f.file).name)}.md"
                frames_dir.joinpath(fname).write_text(_frame_md(i, f), encoding="utf-8")

        # config snapshots
        if w.config_snapshot_paths:
            configs_dir = folder / "configs"
            configs_dir.mkdir()
            for src in w.config_snapshot_paths:
                if src.exists():
                    dest = configs_dir / src.name
                    try:
                        shutil.copy2(src, dest)
                    except OSError as e:
                        log().error(f"failed to snapshot config {src}: {e}")

        # state snapshot
        (folder / "state.json").write_text(
            json.dumps(w.state_snapshot, indent=2, default=str), encoding="utf-8"
        )

        # rule match
        if w.rule_match is not None:
            (folder / "rule_match.json").write_text(
                json.dumps(w.rule_match, indent=2), encoding="utf-8"
            )

        # fix
        fix_dir = folder / "fix"
        fix_dir.mkdir()
        if w.action is not None:
            (fix_dir / "action.json").write_text(json.dumps(w.action, indent=2), encoding="utf-8")
        if w.claude_response:
            (fix_dir / "claude_response.md").write_text(w.claude_response, encoding="utf-8")
        if w.claude_diff:
            (fix_dir / "diff.patch").write_text(w.claude_diff, encoding="utf-8")

        # rendered report
        report_md = render_report(w, folder=folder)
        (folder / "report.md").write_text(report_md, encoding="utf-8")

        # index
        self._append_index(folder, w)
        return folder

    def _append_index(self, folder: Path, w: IncidentWrite) -> None:
        entry = {
            "id": folder.name,
            "kind": w.kind,
            "detector": w.detector,
            "message": w.message,
            "rule": (w.rule_match or {}).get("rule"),
            "action": (w.action or {}).get("kind") if w.action else None,
        }
        with self.index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def _frame_md(i: int, f: ExplodedFrame) -> str:
    head = f"# Frame {i} — `{f.file}:{f.line}` ({f.lang})\n"
    if f.skipped:
        return head + f"\n*Skipped: {f.skipped}*\n\nRaw: `{f.raw.strip()}`\n"
    enc = f"\n**Enclosing:** `{f.enclosing}`\n" if f.enclosing else ""
    src = f"\n```{f.lang}\n{f.source or ''}\n```\n"
    return head + enc + src
