"""VaultStore — the writer side of the Obsidian-compatible vault.

Owns the on-disk layout under :attr:`VaultStore.root` and provides
the public methods the Monitor calls at each significant lifecycle
point. Every write is best-effort: a vault failure logs an error but
never propagates up to the monitor loop (the supervisor must keep
running even if the vault is broken).

File layout::

    <root>/
    ├── index.md
    ├── runs/
    │   ├── <run-id>.md
    │   └── <run-id>/
    │       └── child-<n>.md
    ├── incidents/
    │   ├── <incident-id>.md
    │   └── <incident-id>/
    │       └── attempt-<n>.md
    ├── detectors/
    │   └── <detector>.md
    ├── patterns/
    │   └── <slug>.md
    ├── regressions/
    │   └── <incident-id>.md
    └── exhaustions/
        └── <run-id>.md
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autosentry.logger import log
from autosentry.vault import templates as tpl


@dataclass(frozen=True)
class NoteRef:
    """Handle to a vault note: where it lives on disk and how to
    address it from another note's wikilink."""

    path: Path
    wikilink: str  # the ``name`` part of ``[[name]]``


class VaultStore:
    """Markdown-vault writer.

    Public methods follow the Monitor's lifecycle:

    - :meth:`record_run_start`
    - :meth:`record_child_restart`
    - :meth:`record_incident`
    - :meth:`record_attempt_start`
    - :meth:`record_attempt_resolution`
    - :meth:`record_regression`
    - :meth:`record_exhaustion`

    All writes are best-effort; failures log an error and return
    ``None`` rather than raising.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        # Reentrant so internal helpers like ``_append_list_item`` can
        # acquire it and then call ``_write_atomic`` (which also
        # acquires) without deadlocking. A plain Lock deadlocks the
        # monitor thread on the very first child-restart hook.
        self._lock = threading.RLock()
        self._ensure_layout()

    # ----- layout -----------------------------------------------------------

    def _ensure_layout(self) -> None:
        """Create the directory tree on first use. Idempotent."""
        for sub in (
            "runs",
            "incidents",
            "detectors",
            "patterns",
            "regressions",
            "exhaustions",
        ):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        index = self.root / "index.md"
        if not index.exists():
            self._write_atomic(index, tpl.render_index())

    # ----- run lifecycle ----------------------------------------------------

    def record_run_start(
        self,
        *,
        run_id: str,
        supervisor_kind: str,
        command: list[str],
        started_at: str,
        cwd: str,
    ) -> NoteRef | None:
        """Write the supervisor-session note. Returns the note ref so
        the monitor can wire it into subsequent incident notes."""
        try:
            path = self.root / "runs" / f"{run_id}.md"
            self._write_atomic(
                path,
                tpl.render_run(
                    run_id=run_id,
                    supervisor_kind=supervisor_kind,
                    command=command,
                    started_at=started_at,
                    cwd=cwd,
                ),
            )
            # Pre-create the per-run child-runs subdir so child notes
            # have a home; cheap and means the directory shows up in
            # Obsidian's tree on day one.
            (self.root / "runs" / run_id).mkdir(parents=True, exist_ok=True)
            return NoteRef(path=path, wikilink=run_id)
        except OSError as e:
            log().error(f"vault: failed to write run note {run_id}: {e}")
            return None

    def record_child_restart(
        self,
        *,
        run_id: str,
        child_index: int,
        pid: int | None,
        started_at: str,
        reason: str | None,
    ) -> NoteRef | None:
        """Append a child-process restart under the parent run.

        Also rewrites the run note's "Child restarts" section so the
        graph is consistent without needing a rebuild step.
        """
        try:
            child_id = f"{run_id}-child-{child_index}"
            child_path = self.root / "runs" / run_id / f"child-{child_index}.md"
            child_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_atomic(
                child_path,
                tpl.render_child_run(
                    run_id=run_id,
                    child_index=child_index,
                    pid=pid,
                    started_at=started_at,
                    reason=reason,
                ),
            )
            self._append_list_item(
                self.root / "runs" / f"{run_id}.md",
                section="## Child restarts",
                item=f"- [[{child_id}]] — started {started_at}"
                + (f" ({reason})" if reason else ""),
            )
            return NoteRef(path=child_path, wikilink=child_id)
        except OSError as e:
            log().error(f"vault: failed to write child-run {run_id}/{child_index}: {e}")
            return None

    # ----- incidents --------------------------------------------------------

    def record_incident(
        self,
        *,
        incident_id: str,
        run_id: str,
        child_run_id: str | None,
        detector: str,
        kind: str,
        message: str,
        fired_at: str,
        trace_hash: str | None,
        pattern_slug: str | None,
        incident_folder_rel: str,
    ) -> NoteRef | None:
        """Write the incident summary note + update the detector
        aggregator + (if part of a pattern) the pattern note."""
        try:
            path = self.root / "incidents" / f"{incident_id}.md"
            self._write_atomic(
                path,
                tpl.render_incident(
                    incident_id=incident_id,
                    run_id=run_id,
                    child_run_id=child_run_id,
                    detector=detector,
                    kind=kind,
                    message=message,
                    fired_at=fired_at,
                    trace_hash=trace_hash,
                    pattern_slug=pattern_slug,
                    incident_folder_rel=incident_folder_rel,
                ),
            )
            (self.root / "incidents" / incident_id).mkdir(parents=True, exist_ok=True)
            self._update_detector_aggregator(detector=detector, incident_id=incident_id)
            # Backlink under the run note for fast browsing.
            self._append_list_item(
                self.root / "runs" / f"{run_id}.md",
                section="## Incidents",
                item=f"- [[{incident_id}]] — `{detector}` at {fired_at}",
            )
            if child_run_id:
                self._append_list_item(
                    self.root / "runs" / run_id / f"{child_run_id.rsplit('-', 1)[-1]}.md",
                    section="## Incidents during this child run",
                    item=f"- [[{incident_id}]] — `{detector}` at {fired_at}",
                )
            return NoteRef(path=path, wikilink=incident_id)
        except OSError as e:
            log().error(f"vault: failed to write incident note {incident_id}: {e}")
            return None

    def record_attempt_start(
        self,
        *,
        incident_id: str,
        attempt_index: int,
        source: str,
        rule_name: str | None,
        action_kind: str,
        action_set: dict[str, str],
        started_at: str,
        branch: str | None,
    ) -> NoteRef | None:
        try:
            attempt_id = f"{incident_id}-attempt-{attempt_index}"
            path = self.root / "incidents" / incident_id / f"attempt-{attempt_index}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_atomic(
                path,
                tpl.render_attempt(
                    incident_id=incident_id,
                    attempt_index=attempt_index,
                    source=source,
                    rule_name=rule_name,
                    action_kind=action_kind,
                    action_set=action_set,
                    started_at=started_at,
                    branch=branch,
                ),
            )
            self._append_list_item(
                self.root / "incidents" / f"{incident_id}.md",
                section="## Attempts",
                item=(
                    f"- [[{attempt_id}]] — `{action_kind}` "
                    f"({source}{', rule=' + rule_name if rule_name else ''}) "
                    f"started {started_at}"
                ),
            )
            return NoteRef(path=path, wikilink=attempt_id)
        except OSError as e:
            log().error(f"vault: failed to write attempt note {incident_id}/{attempt_index}: {e}")
            return None

    def record_attempt_resolution(
        self,
        *,
        incident_id: str,
        attempt_index: int,
        status: str,
        ended_at: str,
        notes: str | None = None,
    ) -> None:
        """Update the attempt note's frontmatter status + outcome
        section. Uses a simple text rewrite (the file is tiny)."""
        path = self.root / "incidents" / incident_id / f"attempt-{attempt_index}.md"
        if not path.exists():
            return
        try:
            with self._lock:
                text = path.read_text(encoding="utf-8")
                text = _replace_frontmatter_value(text, "status", status)
                text = _replace_frontmatter_value(text, "updated", _now_iso())
                outcome_block = f"- **Resolved:** {ended_at}\n- **Status:** `{status}`\n" + (
                    f"\n> {notes}\n" if notes else ""
                )
                text = _replace_section(
                    text,
                    section="## Outcome",
                    body=outcome_block,
                )
                self._write_atomic(path, text)
        except OSError as e:
            log().error(
                f"vault: failed to update attempt resolution {incident_id}/{attempt_index}: {e}"
            )

    def replace_narrative(self, note_path: Path, narrative: str) -> None:
        """Swap the templated ``## Narrative`` section's body for the
        given LLM-generated prose. Used by :mod:`autosentry.vault.narrator`
        on first-occurrence events.

        Best-effort: a malformed note (no ``## Narrative`` heading)
        is left unchanged. Idempotent — calling twice with the same
        narrative produces the same file.
        """
        if not note_path.exists():
            return
        try:
            with self._lock:
                text = note_path.read_text(encoding="utf-8")
                # Wrap the narrative paragraph in a single trailing
                # newline so subsequent sections stay separated.
                new_text = _replace_section(
                    text,
                    section="## Narrative",
                    body=narrative.strip() + "\n",
                )
                if new_text != text:
                    new_text = _replace_frontmatter_value(new_text, "updated", _now_iso())
                    self._write_atomic(note_path, new_text)
        except OSError as e:
            log().error(f"vault: failed to replace narrative in {note_path.name}: {e}")

    # ----- significant-event notes -----------------------------------------

    def record_pattern(
        self,
        *,
        slug: str,
        detector: str,
        representative_message: str,
        incident_ids: list[str],
        trace_hash: str | None,
    ) -> NoteRef | None:
        """Create the pattern aggregator note on first sighting; on
        subsequent calls only update the linked-incidents list (and
        the incident_count in frontmatter).

        This preserves any LLM narrative the narrator has injected —
        a full re-render would clobber it back to the templated prose.
        """
        try:
            path = self.root / "patterns" / f"pattern-{slug}.md"
            existed = path.exists()
            if not existed:
                self._write_atomic(
                    path,
                    tpl.render_pattern(
                        slug=slug,
                        detector=detector,
                        representative_message=representative_message,
                        incident_ids=incident_ids,
                        trace_hash=trace_hash,
                    ),
                )
            else:
                # Incremental: append any newly-linked incidents to the
                # ``## Linked incidents`` section + bump the count in
                # frontmatter. Leaves Narrative + other sections alone.
                with self._lock:
                    text = path.read_text(encoding="utf-8")
                    for inc_id in incident_ids:
                        text = _append_under_section(
                            text,
                            "## Linked incidents",
                            f"- [[{inc_id}]]",
                            dedupe=True,
                        )
                    text = _replace_frontmatter_value(
                        text, "incident_count", str(len(incident_ids))
                    )
                    text = _replace_frontmatter_value(text, "updated", _now_iso())
                    self._write_atomic(path, text)
            # Backlink the pattern from the detector aggregator's
            # Patterns section (idempotent).
            self._append_list_item(
                self.root / "detectors" / f"{detector}.md",
                section="## Patterns",
                item=f"- [[pattern-{slug}]]",
                dedupe=True,
            )
            if not existed:
                log().info(f"vault: created pattern note pattern-{slug}")
            return NoteRef(path=path, wikilink=f"pattern-{slug}")
        except OSError as e:
            log().error(f"vault: failed to write pattern note {slug}: {e}")
            return None

    def record_regression(
        self,
        *,
        incident_id: str,
        detector: str,
        original_attempt: int,
        re_fire_at: str,
        feedback: str | None = None,
    ) -> NoteRef | None:
        try:
            path = self.root / "regressions" / f"{incident_id}.md"
            self._write_atomic(
                path,
                tpl.render_regression(
                    incident_id=incident_id,
                    detector=detector,
                    original_attempt=original_attempt,
                    re_fire_at=re_fire_at,
                    feedback=feedback,
                ),
            )
            return NoteRef(path=path, wikilink=f"regression-{incident_id}")
        except OSError as e:
            log().error(f"vault: failed to write regression note {incident_id}: {e}")
            return None

    def record_exhaustion(
        self,
        *,
        run_id: str,
        final_restart_count: int,
        max_restarts: int,
        final_detector: str | None,
        ended_at: str,
    ) -> NoteRef | None:
        try:
            path = self.root / "exhaustions" / f"{run_id}.md"
            self._write_atomic(
                path,
                tpl.render_exhaustion(
                    run_id=run_id,
                    final_restart_count=final_restart_count,
                    max_restarts=max_restarts,
                    final_detector=final_detector,
                    ended_at=ended_at,
                ),
            )
            return NoteRef(path=path, wikilink=f"exhaustion-{run_id}")
        except OSError as e:
            log().error(f"vault: failed to write exhaustion note {run_id}: {e}")
            return None

    # ----- internals --------------------------------------------------------

    def _update_detector_aggregator(self, *, detector: str, incident_id: str) -> None:
        """Append the new incident to the detector aggregator note.
        Creates the note on first use."""
        path = self.root / "detectors" / f"{detector}.md"
        if not path.exists():
            self._write_atomic(path, tpl.render_detector_aggregator(detector))
        self._append_list_item(
            path,
            section="## Incidents",
            item=f"- [[{incident_id}]]",
            dedupe=True,
        )

    def _write_atomic(self, path: Path, content: str) -> None:
        """Atomic write via temp + ``os.replace``. Mirrors the same
        trick :class:`autosentry.state.StateStore` uses. On iCloud-
        synced directories the rename can race with the evictor; we
        fall back to a direct write on FileNotFoundError so a vault
        write never permanently fails."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with self._lock:
            try:
                with tmp.open("w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except FileNotFoundError:
                with path.open("w", encoding="utf-8") as f:
                    f.write(content)

    def _append_list_item(
        self,
        path: Path,
        *,
        section: str,
        item: str,
        dedupe: bool = False,
    ) -> None:
        """Append a bullet under a markdown section heading.

        If the section's body is currently the placeholder
        ``*(none yet)*`` (or similar), replace it with the new item.
        Otherwise append after the existing items, before the next
        ``##`` heading. With ``dedupe=True``, skip if the item is
        already present in the section.
        """
        if not path.exists():
            return
        try:
            with self._lock:
                text = path.read_text(encoding="utf-8")
                new_text = _append_under_section(text, section, item, dedupe=dedupe)
                if new_text != text:
                    new_text = _replace_frontmatter_value(new_text, "updated", _now_iso())
                    self._write_atomic(path, new_text)
        except OSError as e:
            log().error(f"vault: failed to update {path.name} section {section!r}: {e}")


# ---------------------------------------------------------------------------
# Module-private helpers (also used by tests)
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _replace_frontmatter_value(text: str, key: str, new_value: str) -> str:
    """Replace ``<key>: <old>`` inside the YAML frontmatter. No-op if
    the key isn't present."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return text
    fm = m.group(1)
    new_fm, n = re.subn(
        rf"(?m)^{re.escape(key)}: .*$",
        f"{key}: {new_value}",
        fm,
        count=1,
    )
    if n == 0:
        return text
    return text.replace(m.group(0), f"---\n{new_fm}\n---\n", 1)


_PLACEHOLDER_RE = re.compile(r"\*\(none yet\)\*")


def _append_under_section(text: str, section: str, item: str, *, dedupe: bool) -> str:
    """Insert ``item`` immediately after ``section`` heading or at the
    end of that section's body (before the next ``## `` heading).

    Section bodies that currently hold the ``*(none yet)*`` placeholder
    are replaced; existing item lists get an append. ``dedupe=True``
    short-circuits if ``item`` already appears in the section.
    """
    lines = text.split("\n")
    try:
        section_idx = next(i for i, line in enumerate(lines) if line.strip() == section)
    except StopIteration:
        return text
    # Find the end of this section: the next ``## `` heading or EOF.
    end_idx = len(lines)
    for j in range(section_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end_idx = j
            break
    section_body = lines[section_idx + 1 : end_idx]
    if dedupe and any(item.strip() == ln.strip() for ln in section_body):
        return text
    # Replace placeholder if present.
    new_body: list[str]
    if any(_PLACEHOLDER_RE.search(ln) for ln in section_body):
        new_body = [item]
    else:
        # Strip trailing blank lines so the appended item nests neatly
        # under the existing list.
        trimmed = list(section_body)
        while trimmed and not trimmed[-1].strip():
            trimmed.pop()
        trimmed.append(item)
        new_body = trimmed
    return "\n".join(lines[: section_idx + 1] + [""] + new_body + [""] + lines[end_idx:])


def _replace_section(text: str, *, section: str, body: str) -> str:
    """Replace everything between ``section`` heading and the next
    ``## `` (exclusive) with ``body``. Leaves the heading itself."""
    lines = text.split("\n")
    try:
        section_idx = next(i for i, line in enumerate(lines) if line.strip() == section)
    except StopIteration:
        return text
    end_idx = len(lines)
    for j in range(section_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end_idx = j
            break
    new_lines = (
        lines[: section_idx + 1] + [""] + body.rstrip("\n").split("\n") + [""] + lines[end_idx:]
    )
    return "\n".join(new_lines)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# pyrefly: ignore Any import lint
_ = Any
