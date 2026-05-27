"""Dispatcher daemon — outbox drain + inbox pump.

The daemon is one process. It owns a ``DispatcherState`` (persisted as
``.autosentry/dispatcher_state.json``) that tracks two things:

1. ``thread_ts`` per ``thread_key`` — the parent message ts in Slack so
   subsequent outbox entries thread under it.
2. ``last_seen_reply_ts`` per ``thread_key`` — so inbound polling only
   pulls replies the monitor hasn't already seen.

The outbox file is rewritten atomically after each loop iteration with
``sent: true`` and ``thread_ts`` populated. The inbox file is
append-only.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from autosentry.dispatcher.backends import Backend
from autosentry.logger import log

# ----- data classes --------------------------------------------------------


@dataclass
class OutboxEntry:
    """One queued message from the monitor's slack notifier."""

    id: str
    channel: str | None
    thread_key: str | None
    kind: str
    title: str
    body: str
    incident_id: str | None = None
    sent: bool = False
    thread_ts: str | None = None
    delivered_at: str | None = None
    error: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> OutboxEntry:
        # The notifier writes a richer dict; we only require these keys.
        return cls(
            id=str(raw.get("id") or ""),
            channel=raw.get("channel"),
            thread_key=raw.get("thread_key"),
            kind=str(raw.get("kind") or "info"),
            title=str(raw.get("title") or ""),
            body=str(raw.get("body") or ""),
            incident_id=raw.get("incident_id"),
            sent=bool(raw.get("sent", False)),
            thread_ts=raw.get("thread_ts"),
            delivered_at=raw.get("delivered_at"),
            error=raw.get("error"),
        )


@dataclass
class InboxEntry:
    """One human reply observed in a Slack thread."""

    id: str
    ts: str
    thread_key: str | None
    channel: str | None
    user: str
    text: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    processed: bool = False


@dataclass
class DispatcherState:
    """Persistent state for the dispatcher daemon."""

    threads: dict[str, str] = field(default_factory=dict)  # thread_key → parent ts
    last_seen_reply: dict[str, str] = field(default_factory=dict)  # thread_key → ts
    bot_user_id: str | None = None

    @classmethod
    def load(cls, path: Path) -> DispatcherState:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls(
            threads=data.get("threads") or {},
            last_seen_reply=data.get("last_seen_reply") or {},
            bot_user_id=data.get("bot_user_id"),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)


# ----- inbound command parsing ---------------------------------------------

# A small grammar — keep it boring and obvious. Commands are recognized at
# the start of the message (ignoring leading whitespace). Anything that
# doesn't match a command verb falls through to a free-form "comment".
_VERBS = {"abort", "pause", "resume", "approve"}
_SET_RE = re.compile(r"^\s*set\s+([a-z_]+)\s+(.+?)\s*$", re.IGNORECASE)
_COMMENT_RE = re.compile(r"^\s*comment\s*:\s*(.+?)\s*$", re.IGNORECASE)


def parse_command(text: str) -> tuple[str | None, list[str]]:
    """Return ``(command, args)`` for a human reply, or ``(None, [])``.

    Commands::

        abort | pause | resume | approve  → ("abort", []) etc.
        set <key> <value>                 → ("set", ["<key>", "<value>"])
        comment: <free text>              → ("comment", ["<free text>"])

    Plain prose returns ``(None, [])`` and the dispatcher treats it as a
    background comment (still appended to slack_inbox.jsonl, just without
    a command field).
    """
    if not text:
        return None, []
    stripped = text.strip()
    lowered = stripped.lower()
    for v in _VERBS:
        if lowered == v or lowered.startswith(f"{v} "):
            tail = stripped[len(v) :].strip()
            return v, [tail] if tail else []
    m = _SET_RE.match(stripped)
    if m:
        return "set", [m.group(1).lower(), m.group(2)]
    m = _COMMENT_RE.match(stripped)
    if m:
        return "comment", [m.group(1)]
    return None, []


# ----- daemon ---------------------------------------------------------------


class DispatcherDaemon:
    """Long-running daemon loop. ``run()`` blocks until stop() is called.

    The loop is intentionally lazy:

    - **Outbox** is rewound only when its mtime advances since the last
      successful drain. Idle iterations cost one ``stat`` call.
    - **Inbound polling** fires only when the inbound marker file mtime
      advances OR ``idle_inbound_seconds`` have elapsed since the last
      Slack call. The monitor touches the marker on each detection fire,
      so the dispatcher is reactive to anomaly events without an ambient
      polling loop. The idle interval is a safety net for commands sent
      during quiet stretches.
    """

    def __init__(
        self,
        *,
        backend: Backend,
        outbox_path: Path,
        inbox_path: Path,
        state_path: Path,
        poll_seconds: float = 30.0,
        default_channel: str | None = None,
        inbound_marker_path: Path | None = None,
        idle_inbound_seconds: float = 300.0,
    ) -> None:
        self.backend = backend
        self.outbox_path = outbox_path
        self.inbox_path = inbox_path
        self.state_path = state_path
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.default_channel = default_channel
        self.inbound_marker_path = inbound_marker_path
        self.idle_inbound_seconds = max(0.0, float(idle_inbound_seconds))
        self.state = DispatcherState.load(state_path)
        self._stop = threading.Event()
        # Idle-state trackers.
        self._last_outbox_mtime: float = 0.0
        self._last_marker_mtime: float = 0.0
        self._last_inbound_poll: float = 0.0

    # ----- lifecycle -------------------------------------------------------

    def run(self) -> None:
        log().info(
            f"dispatcher: backend={self.backend.name} outbox={self.outbox_path} "
            f"inbox={self.inbox_path} poll={self.poll_seconds}s"
        )
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception as e:  # noqa: BLE001
                log().error(f"dispatcher tick failed: {e}")
            self._stop.wait(self.poll_seconds)
        log().info("dispatcher stopped")

    def stop(self) -> None:
        self._stop.set()

    # ----- one iteration ---------------------------------------------------

    def tick_once(self) -> None:
        """One iteration: drain outbox if its mtime advanced, poll Slack if
        the inbound marker advanced (or the idle interval elapsed)."""
        # ----- outbox: mtime-gated drain ---------------------------------
        outbox_mtime = _mtime_or_zero(self.outbox_path)
        if outbox_mtime > self._last_outbox_mtime:
            entries = self._load_outbox()
            changed = False
            for e in entries:
                if e.sent:
                    continue
                channel = e.channel or self.default_channel
                thread_ts = self.state.threads.get(e.thread_key) if e.thread_key else None
                text = _format_outbound(e)
                result = self.backend.deliver(channel=channel, text=text, thread_ts=thread_ts)
                e.delivered_at = _now_iso()
                if result.ok:
                    e.sent = True
                    e.thread_ts = result.ts
                    e.error = None
                    if e.thread_key and result.ts and e.thread_key not in self.state.threads:
                        # First successful message under this thread_key —
                        # pin its ts as the parent so later entries thread.
                        self.state.threads[e.thread_key] = result.ts
                else:
                    e.error = result.error or "unknown"
                    log().error(f"dispatcher: delivery failed for {e.id}: {e.error}")
                changed = True

            if changed:
                self._save_outbox(entries)
                self.state.save(self.state_path)
            # Re-stat AFTER our own rewrite so we don't immediately re-drain.
            self._last_outbox_mtime = _mtime_or_zero(self.outbox_path)

        # ----- inbound: marker-gated + idle sweep ------------------------
        if self.backend.supports_inbound():
            should_poll = False
            if self.inbound_marker_path is not None:
                marker_mtime = _mtime_or_zero(self.inbound_marker_path)
                if marker_mtime > self._last_marker_mtime:
                    should_poll = True
                    self._last_marker_mtime = marker_mtime
            else:
                # No marker configured → always poll on every tick (Phase 3 behavior).
                should_poll = True

            if (
                not should_poll
                and self.idle_inbound_seconds > 0
                and (time.monotonic() - self._last_inbound_poll) >= self.idle_inbound_seconds
            ):
                # Idle sweep: ensures replies sent during quiet stretches
                # still land eventually.
                should_poll = True

            if should_poll:
                self._poll_inbound()
                self._last_inbound_poll = time.monotonic()

    # ----- outbox I/O ------------------------------------------------------

    def _load_outbox(self) -> list[OutboxEntry]:
        if not self.outbox_path.exists():
            return []
        out: list[OutboxEntry] = []
        with self.outbox_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(OutboxEntry.from_dict(json.loads(line)))
                except json.JSONDecodeError:
                    log().error(f"dispatcher: skipping malformed outbox line: {line[:80]!r}")
        return out

    def _save_outbox(self, entries: list[OutboxEntry]) -> None:
        tmp = self.outbox_path.with_suffix(self.outbox_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(asdict(e)) + "\n")
        os.replace(tmp, self.outbox_path)

    # ----- inbox I/O -------------------------------------------------------

    def _poll_inbound(self) -> None:
        if not self.state.threads:
            return
        new_records: list[InboxEntry] = []
        for thread_key, parent_ts in self.state.threads.items():
            replies = self.backend.fetch_replies(
                channel=self.default_channel,  # may be None for stdout etc.
                thread_ts=parent_ts,
                after_ts=self.state.last_seen_reply.get(thread_key),
            )
            for r in replies:
                cmd, args = parse_command(r.text)
                new_records.append(
                    InboxEntry(
                        id=f"{r.ts}:{thread_key}",
                        ts=r.ts,
                        thread_key=thread_key,
                        channel=self.default_channel,
                        user=r.user,
                        text=r.text,
                        command=cmd,
                        args=args,
                    )
                )
                self.state.last_seen_reply[thread_key] = r.ts
        if new_records:
            self._append_inbox(new_records)
            self.state.save(self.state_path)

    def _append_inbox(self, records: list[InboxEntry]) -> None:
        self.inbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self.inbox_path.open("a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(asdict(r)) + "\n")


# ----- helpers -------------------------------------------------------------


def _format_outbound(e: OutboxEntry) -> str:
    """Render an outbox entry as a Slack-flavored message body."""
    parts: list[str] = []
    parts.append(f"*[{e.kind}]* {e.title}")
    if e.body:
        parts.append(e.body)
    if e.incident_id:
        parts.append(f"_incident: `{e.incident_id}`_")
    return "\n".join(parts)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _mtime_or_zero(path: Path | None) -> float:
    """``stat().st_mtime`` for a file, or 0.0 if it doesn't exist.

    Used as a cheap "has this file changed?" signal — one syscall per
    check, no read of the contents.
    """
    if path is None:
        return 0.0
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0
    except OSError as e:
        log().error(f"dispatcher: stat({path}) failed: {e}")
        return 0.0
