"""Monitor-side consumer of ``slack_inbox.jsonl``.

The dispatcher writes parsed human replies to this file. The monitor
reads the file once per tick (cheap, local IO only) and applies any
recognized commands to its state and supervisor.

Why this lives in the monitor (not the dispatcher): commands like
``abort`` and ``pause`` need direct control over the supervised process,
which only the monitor has. The dispatcher's job stops at "translate
Slack into structured commands and persist them"; the monitor's job is
to apply them.

Read-only contract for ``slack_inbox.jsonl``:

- Append-only — the dispatcher only ever appends.
- Each line is a JSON object matching :class:`InboxEntry` (defined in
  ``autosentry.dispatcher.daemon``). The monitor only needs ``id``,
  ``user``, ``text``, ``command``, ``args``.
- ``state.last_processed_inbox_id`` tracks how far we've consumed so we
  don't re-apply commands across monitor restarts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from autosentry.logger import log

if TYPE_CHECKING:
    from autosentry.monitor import Monitor


@dataclass(frozen=True)
class InboxRecord:
    """Slim subset of the dispatcher's ``InboxEntry`` for the monitor's use."""

    id: str
    user: str
    text: str
    command: str | None
    args: list[str]


def read_new(inbox_path: Path, after_id: str | None) -> list[InboxRecord]:
    """Return inbox records that arrived after ``after_id``.

    Returns an empty list if the file doesn't exist yet. Tolerant of
    malformed lines — they're logged and skipped.
    """
    if not inbox_path.exists():
        return []
    out: list[InboxRecord] = []
    seen_cursor = after_id is None
    with inbox_path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                log().error(f"inbox: skipping malformed line: {line[:80]!r}")
                continue
            rec = InboxRecord(
                id=str(data.get("id") or ""),
                user=str(data.get("user") or ""),
                text=str(data.get("text") or ""),
                command=data.get("command"),
                args=list(data.get("args") or []),
            )
            if not seen_cursor:
                if rec.id == after_id:
                    seen_cursor = True
                continue
            out.append(rec)
    return out


# ----- command application -------------------------------------------------


def apply_commands(monitor: Monitor, inbox_path: Path) -> int:
    """Read new inbox records and apply recognized commands. Returns count
    of records applied. Updates ``state.user["last_processed_inbox_id"]``.
    """
    after = monitor.state.user.get("last_processed_inbox_id")
    new = read_new(inbox_path, after)
    if not new:
        return 0
    for rec in new:
        _apply_one(monitor, rec)
    monitor.state.user["last_processed_inbox_id"] = new[-1].id
    return len(new)


def _apply_one(monitor: Monitor, rec: InboxRecord) -> None:
    """Apply a single inbox record to monitor + supervisor."""
    cmd = (rec.command or "").lower()
    args = rec.args
    if cmd == "abort":
        log().recovery(f"inbox: ABORT from {rec.user!r} — stopping monitor")
        monitor._stop = True
        try:
            monitor.supervisor.stop()
        except Exception as e:  # noqa: BLE001
            log().error(f"inbox: supervisor.stop() failed: {e}")
        return

    if cmd == "pause":
        log().recovery(f"inbox: PAUSE from {rec.user!r} — stopping supervisor")
        try:
            monitor.supervisor.stop()
        except Exception as e:  # noqa: BLE001
            log().error(f"inbox: supervisor.stop() failed: {e}")
        return

    if cmd == "resume":
        log().recovery(f"inbox: RESUME from {rec.user!r} — starting supervisor")
        try:
            monitor.supervisor.start()
        except Exception as e:  # noqa: BLE001
            log().error(f"inbox: supervisor.start() failed: {e}")
        return

    if cmd == "set" and len(args) >= 2:
        key, value = args[0], args[1]
        _apply_set(monitor, rec.user, key, value)
        return

    if cmd == "approve":
        # Placeholder for a future Claude-approval flow. For now, just record.
        log().recovery(f"inbox: APPROVE from {rec.user!r} (recorded)")
        monitor.state.user.setdefault("approvals", []).append(
            {"id": rec.id, "user": rec.user, "text": rec.text}
        )
        return

    if cmd == "comment":
        text = args[0] if args else rec.text
        log().info(f"inbox: comment from {rec.user!r}: {text}")
        monitor.state.user.setdefault("comments", []).append(
            {"id": rec.id, "user": rec.user, "text": text}
        )
        return

    if cmd:
        log().error(f"inbox: unrecognized command {cmd!r} from {rec.user!r} — ignored")
    # else: prose with no recognized command; logged at info via the dispatcher;
    # the monitor doesn't need to act.


def _apply_set(monitor: Monitor, user: str, key: str, value: str) -> None:
    """Apply ``set <key> <value>`` to the monitor's state."""
    key_l = key.lower()
    if key_l == "max_restarts":
        try:
            n = int(value)
        except ValueError:
            log().error(f"inbox: SET max_restarts from {user!r}: invalid value {value!r}")
            return
        old = monitor.state.max_restarts
        monitor.state.max_restarts = n
        log().recovery(f"inbox: SET max_restarts {old} → {n} from {user!r}")
        return
    # Unknown keys go into the user-extensible bucket — rules can read them.
    log().recovery(f"inbox: SET user.{key_l} = {value!r} from {user!r}")
    monitor.state.user[f"set_{key_l}"] = value
