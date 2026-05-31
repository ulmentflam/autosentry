"""Markdown templates for vault notes.

Every template is a deterministic f-string-style render of structured
data — no LLM, no Jinja, no runtime template loader. Each function
returns the full file body including frontmatter.

Frontmatter convention::

    ---
    id: <stable identifier, slug form>
    kind: run | child-run | incident | attempt | detector | pattern
          | regression | exhaustion | index
    created: <ISO-8601 UTC>
    updated: <ISO-8601 UTC, equal to created on first write>
    tags: [<list of single-token tags Obsidian can index>]
    <kind-specific scalar fields>
    ---

Notes use Obsidian-style wikilinks (``[[note-name]]``) so the vault
renders correctly when opened in Obsidian without any plugin setup.
The graph view in ``autosentry web`` (v0.12.0) parses these.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _format_tags(tags: list[str]) -> str:
    """Render Obsidian-compatible inline tags. Empty list -> empty string."""
    if not tags:
        return "[]"
    return "[" + ", ".join(tags) + "]"


def _frontmatter(
    *,
    id_: str,
    kind: str,
    tags: list[str],
    extras: dict[str, Any] | None = None,
) -> str:
    """Build a YAML frontmatter block. Keys are emitted in stable order
    so notes diff cleanly across updates."""
    now = _now_iso()
    lines = [
        "---",
        f"id: {id_}",
        f"kind: {kind}",
        f"created: {now}",
        f"updated: {now}",
        f"tags: {_format_tags(tags)}",
    ]
    if extras:
        for k, v in sorted(extras.items()):
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level index
# ---------------------------------------------------------------------------


def render_index() -> str:
    """The vault home page. Created once; not auto-updated."""
    return (
        _frontmatter(id_="index", kind="index", tags=["index"])
        + "\n\n"
        + "# autosentry vault\n\n"
        + "Welcome. This vault is autosentry's record of significant supervisor "
        + "events, rendered as Obsidian-compatible markdown.\n\n"
        + "## Where to look\n\n"
        + "- **[[runs]]** — every supervisor session, broken down into its "
        + "child-process restarts.\n"
        + "- **[[incidents]]** — every detection that fired, with its fix-"
        + "attempt sub-tree.\n"
        + "- **[[detectors]]** — per-detector aggregators (which detectors "
        + "fire the most? when do their fixes stick?).\n"
        + "- **[[patterns]]** — recurring failure modes autosentry has "
        + "identified across multiple incidents.\n"
        + "- **[[regressions]]** — fixes that re-fired inside the "
        + "verify window.\n"
        + "- **[[exhaustions]]** — runs that hit `max_restarts` and gave up.\n\n"
        + "## Conventions\n\n"
        + "- Wikilinks (`[[name]]`) connect notes; open this folder in "
        + "Obsidian for the graph view.\n"
        + "- Notes are written incrementally as events happen; nothing here "
        + "is auto-deleted.\n"
        + "- The full forensic artifacts (stack frames, configs, diffs) live "
        + "under `.autosentry/incidents/<id>/`; the incident note links to "
        + "those files directly.\n"
    )


# ---------------------------------------------------------------------------
# Run notes
# ---------------------------------------------------------------------------


def render_run(
    *,
    run_id: str,
    supervisor_kind: str,
    command: list[str],
    started_at: str,
    cwd: str,
) -> str:
    cmd_repr = " ".join(command)
    extras = {
        "started_at": started_at,
        "supervisor_kind": supervisor_kind,
    }
    body = (
        _frontmatter(
            id_=run_id,
            kind="run",
            tags=["run", f"supervisor/{supervisor_kind}"],
            extras=extras,
        )
        + "\n\n"
        + f"# Run `{run_id}`\n\n"
        + f"- **Started:** {started_at}\n"
        + f"- **Supervisor:** `{supervisor_kind}`\n"
        + f"- **Command:** `{cmd_repr}`\n"
        + f"- **Working directory:** `{cwd}`\n"
        + "- **Exit:** *(open; will be filled in when the run ends)*\n\n"
        + "## Narrative\n\n"
        + f"autosentry started supervising `{cmd_repr}` at {started_at}. "
        + "Each time the supervised process restarts, a child-run note is added "
        + "below; each detection becomes an [[incidents|incident]].\n\n"
        + "## Child restarts\n\n"
        + "*(none yet)*\n\n"
        + "## Incidents\n\n"
        + "*(none yet)*\n"
    )
    return body


def render_child_run(
    *,
    run_id: str,
    child_index: int,
    pid: int | None,
    started_at: str,
    reason: str | None,
) -> str:
    child_id = f"{run_id}-child-{child_index}"
    reason_block = f"- **Started because:** {reason}\n" if reason else ""
    pid_block = f"- **PID:** {pid}\n" if pid is not None else "- **PID:** *(unknown)*\n"
    return (
        _frontmatter(
            id_=child_id,
            kind="child-run",
            tags=["run", "child-run"],
            extras={"parent_run": run_id, "child_index": child_index},
        )
        + "\n\n"
        + f"# Child run #{child_index} of [[{run_id}]]\n\n"
        + f"- **Started:** {started_at}\n"
        + pid_block
        + reason_block
        + "\n"
        + "## Incidents during this child run\n\n"
        + "*(none yet)*\n"
    )


# ---------------------------------------------------------------------------
# Incident + attempt notes
# ---------------------------------------------------------------------------


def render_incident(
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
) -> str:
    """Per-incident summary linking to its parent run, child-run,
    detector aggregator, and (if it's part of one) pattern note.

    The full forensic data (stack frames, exploded source, config
    snapshots, diff) stays under ``incidents/<id>/`` — this note is
    the *summary* that wikilinks the surrounding context.
    """
    parent_links = f"[[{run_id}]]"
    if child_run_id:
        parent_links += f" -> [[{child_run_id}]]"
    pattern_block = f"- **Pattern:** [[pattern-{pattern_slug}]]\n" if pattern_slug else ""
    trace_block = (
        f"- **Trace fingerprint:** `{trace_hash}` (used to identify same-bug crashes)\n"
        if trace_hash
        else ""
    )
    return (
        _frontmatter(
            id_=incident_id,
            kind="incident",
            tags=["incident", f"detector/{detector}", f"kind/{kind}"],
            extras={
                "detector": detector,
                "detection_kind": kind,
                "fired_at": fired_at,
                "run": run_id,
            },
        )
        + "\n\n"
        + f"# Incident `{incident_id}`\n\n"
        + f"- **Run:** {parent_links}\n"
        + f"- **Detector:** [[detector-{detector}]] (`{kind}`)\n"
        + f"- **Fired at:** {fired_at}\n"
        + f"- **Message:** `{_one_line(message)}`\n"
        + trace_block
        + pattern_block
        + f"- **Forensic data:** [{incident_folder_rel}](../../{incident_folder_rel}/report.md)\n\n"
        + "## Narrative\n\n"
        + f"The `{detector}` detector fired at {fired_at} on run {parent_links}.\n"
        + f"The detection's message was: `{_one_line(message)}`.\n\n"
        + "## Attempts\n\n"
        + "*(filled in as the healer tries fixes)*\n"
    )


def render_attempt(
    *,
    incident_id: str,
    attempt_index: int,
    source: str,
    rule_name: str | None,
    action_kind: str,
    action_set: dict[str, str],
    started_at: str,
    branch: str | None,
) -> str:
    attempt_id = f"{incident_id}-attempt-{attempt_index}"
    set_block = ""
    if action_set:
        pairs = ", ".join(f"`{k}={v}`" for k, v in action_set.items())
        set_block = f"- **Env overrides:** {pairs}\n"
    rule_block = f"- **Rule:** `{rule_name}`\n" if rule_name else ""
    branch_block = f"- **Fix branch:** `{branch}`\n" if branch else ""
    return (
        _frontmatter(
            id_=attempt_id,
            kind="attempt",
            tags=["attempt", f"source/{source}", f"action/{action_kind}"],
            extras={
                "incident": incident_id,
                "attempt_index": attempt_index,
                "source": source,
                "action_kind": action_kind,
                "started_at": started_at,
                "status": "pending",
            },
        )
        + "\n\n"
        + f"# Attempt #{attempt_index} for [[{incident_id}]]\n\n"
        + f"- **Source:** `{source}`\n"
        + rule_block
        + f"- **Action:** `{action_kind}`\n"
        + set_block
        + branch_block
        + f"- **Started:** {started_at}\n"
        + "- **Status:** *(pending)*\n\n"
        + "## Outcome\n\n"
        + "*(will be filled in when the verify window elapses)*\n"
    )


# ---------------------------------------------------------------------------
# Aggregator notes
# ---------------------------------------------------------------------------


def render_detector_aggregator(detector: str) -> str:
    """One per detector. Created on first incident; updated on each
    subsequent fire to append to the incident list."""
    return (
        _frontmatter(
            id_=f"detector-{detector}",
            kind="detector",
            tags=["detector", f"detector/{detector}"],
            extras={"detector": detector},
        )
        + "\n\n"
        + f"# Detector `{detector}`\n\n"
        + "Every incident this detector has produced, in chronological order. "
        + "If autosentry has identified recurring patterns within this detector, "
        + "they appear in the **Patterns** section below.\n\n"
        + "## Patterns\n\n"
        + "*(none yet)*\n\n"
        + "## Incidents\n\n"
        + "*(none yet)*\n"
    )


def render_pattern(
    *,
    slug: str,
    detector: str,
    representative_message: str,
    incident_ids: list[str],
    trace_hash: str | None,
) -> str:
    incident_links = "\n".join(f"- [[{i}]]" for i in incident_ids) or "*(none yet)*"
    trace_block = f"- **Trace fingerprint:** `{trace_hash}`\n" if trace_hash else ""
    return (
        _frontmatter(
            id_=f"pattern-{slug}",
            kind="pattern",
            tags=["pattern", f"detector/{detector}"],
            extras={
                "detector": detector,
                "slug": slug,
                "incident_count": len(incident_ids),
            },
        )
        + "\n\n"
        + f"# Pattern `{slug}`\n\n"
        + f"- **Detector:** [[detector-{detector}]]\n"
        + f"- **Representative message:** `{_one_line(representative_message)}`\n"
        + trace_block
        + f"- **Incidents observed:** {len(incident_ids)}\n\n"
        + "## Narrative\n\n"
        + f"autosentry has seen this failure mode {len(incident_ids)} time(s). "
        + "The fix history under each incident shows whether deterministic "
        + "rules or LLM-driven recovery has been most effective.\n\n"
        + "## Linked incidents\n\n"
        + incident_links
        + "\n"
    )


def render_regression(
    *,
    incident_id: str,
    detector: str,
    original_attempt: int,
    re_fire_at: str,
    feedback: str | None,
) -> str:
    feedback_block = f"\n> Cross-check feedback: {feedback}\n" if feedback else ""
    return (
        _frontmatter(
            id_=f"regression-{incident_id}",
            kind="regression",
            tags=["regression", f"detector/{detector}"],
            extras={
                "incident": incident_id,
                "original_attempt": original_attempt,
                "re_fire_at": re_fire_at,
            },
        )
        + "\n\n"
        + f"# Regression on [[{incident_id}]]\n\n"
        + f"- **Detector:** [[detector-{detector}]]\n"
        + f"- **Original attempt:** [[{incident_id}-attempt-{original_attempt}]]\n"
        + f"- **Re-fired at:** {re_fire_at}\n\n"
        + "## Narrative\n\n"
        + "The fix applied for this incident didn't stick — the same detector "
        + f"re-fired at {re_fire_at}, inside the verify window. autosentry "
        + "rolled back the fix branch and (per `healing.escalate_on_rule_"
        + "regression`) routes the next attempt straight to the agentic healer.\n"
        + feedback_block
    )


def render_exhaustion(
    *,
    run_id: str,
    final_restart_count: int,
    max_restarts: int,
    final_detector: str | None,
    ended_at: str,
) -> str:
    detector_block = (
        f"- **Last unresolved detector:** [[detector-{final_detector}]]\n" if final_detector else ""
    )
    return (
        _frontmatter(
            id_=f"exhaustion-{run_id}",
            kind="exhaustion",
            tags=["exhaustion", "give-up"],
            extras={
                "run": run_id,
                "final_restart_count": final_restart_count,
                "max_restarts": max_restarts,
                "ended_at": ended_at,
            },
        )
        + "\n\n"
        + f"# Recovery exhaustion on [[{run_id}]]\n\n"
        + f"- **Restarts used:** {final_restart_count}/{max_restarts}\n"
        + detector_block
        + f"- **Ended:** {ended_at}\n\n"
        + "## Narrative\n\n"
        + "autosentry burned through its `max_restarts` budget of "
        + f"{max_restarts} on this run without a kept fix and stopped the "
        + "supervisor. A service manager (systemd / launchd / supervisord) "
        + "should be the next layer; that's also where you decide whether "
        + "to relaunch autosentry with a different config.\n"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _one_line(s: str, max_len: int = 240) -> str:
    """Compact multi-line strings into a single line for inline display."""
    flat = " ".join(s.split())
    return flat if len(flat) <= max_len else flat[: max_len - 1] + "…"
