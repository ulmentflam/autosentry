"""Monitor state — persisted to a JSON file via atomic write.

State is the single source of truth across restarts and across the
boundary between the monitor and any Claude session it spawns. Reads
and writes happen through a tiny ``StateStore`` that wraps the JSON
file with atomic-rename semantics so concurrent readers never see
torn writes.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class AnomalyRecord(BaseModel):
    time: str
    detector: str
    message: str
    incident_id: str | None = None


class RestartRecord(BaseModel):
    time: str
    reason: str
    rule: str | None = None
    new_pid: int | None = None
    incident_id: str | None = None


class ResetRecord(BaseModel):
    """An audit row for one `autosentry reset` (or inter-stage reset) event.

    Captures the counters as they stood at reset time so the reason is
    legible after the fact — operators can answer "why did the budget
    look full at 3am? someone reset; here's what was cleared".
    """

    time: str
    reason: str
    prior_restarts: int
    prior_restarts_total: int
    cleared_history: bool = False
    stage: str | None = None  # set when triggered between pipeline stages
    source: str = "manual"  # "manual" | "pipeline" | "test"


class MonitorState(BaseModel):
    """In-memory shape of state.json. Anything not in here doesn't persist."""

    # Process tracking
    pid: int | None = None
    started_at: str | None = None
    last_heartbeat: str | None = None
    last_exit_code: int | None = None

    # Progress
    last_progress_value: str | None = None
    last_progress_at: str | None = None

    # Restart bookkeeping
    # Unverified-restart counter. Resets to 0 whenever a fix attempt
    # resolves as ``kept`` in the attempts ledger. Used for both the
    # give-up check (see :func:`budget_exhausted`) and the
    # force-Claude escalation threshold. Successful heals shouldn't
    # consume budget — new runs after a heal start with a fresh slate.
    restarts: int = 0
    # All-time counter; never resets. Audit-only.
    restarts_total: int = 0
    # Overridden from ``cfg.process.restart_policy`` on Monitor.__init__;
    # the authoritative default lives in ``config.RestartPolicy.max_restarts``.
    # This counter is the unverified-restart *kill-switch*, not a raw
    # cap — see :func:`budget_exhausted`. ``0`` disables it entirely.
    max_restarts: int = 50
    last_restart_at: str | None = None
    last_recovery_failed_for: str | None = None  # set when claude fix also failed
    restart_history: list[RestartRecord] = Field(default_factory=list)
    # Manual + inter-stage resets. Capped at 200 like the others; older
    # entries roll off. Plain-text mirror lives at .autosentry/reset.log.
    reset_history: list[ResetRecord] = Field(default_factory=list)

    # Anomalies (rolling window of recent ones, capped)
    anomalies: list[AnomalyRecord] = Field(default_factory=list)
    anomaly_last_notified: dict[str, str] = Field(default_factory=dict)

    # Notifier bookkeeping
    slack_thread_ts: str | None = None
    slack_milestones_sent: list[str] = Field(default_factory=list)

    # User-extensible bucket — anything custom rules want to remember
    user: dict[str, Any] = Field(default_factory=dict)

    def record_anomaly(self, detector: str, message: str, incident_id: str | None = None) -> None:
        self.anomalies.append(
            AnomalyRecord(time=_now(), detector=detector, message=message, incident_id=incident_id)
        )
        if len(self.anomalies) > 200:
            self.anomalies = self.anomalies[-200:]

    def record_restart(
        self,
        reason: str,
        *,
        rule: str | None = None,
        new_pid: int | None = None,
        incident_id: str | None = None,
    ) -> None:
        self.restarts += 1
        self.restarts_total += 1
        self.last_restart_at = _now()
        self.restart_history.append(
            RestartRecord(
                time=self.last_restart_at,
                reason=reason,
                rule=rule,
                new_pid=new_pid,
                incident_id=incident_id,
            )
        )
        if len(self.restart_history) > 200:
            self.restart_history = self.restart_history[-200:]

    def record_reset(
        self,
        reason: str,
        *,
        clear_history: bool = False,
        stage: str | None = None,
        source: str = "manual",
    ) -> ResetRecord:
        """Capture counters, append a ResetRecord, then zero what was asked.

        Always clears ``self.restarts`` (the unverified-restart budget,
        which is the whole point). ``restarts_total`` is preserved as an
        audit-only running total. ``restart_history`` is preserved by
        default; pass ``clear_history=True`` to drop it (the operator
        opted into a full wipe with --full).

        Returns the ResetRecord that was appended — callers (CLI,
        pipeline runner) can mirror it to ``reset.log`` for ops to tail.
        """
        record = ResetRecord(
            time=_now(),
            reason=reason,
            prior_restarts=self.restarts,
            prior_restarts_total=self.restarts_total,
            cleared_history=clear_history,
            stage=stage,
            source=source,
        )
        self.reset_history.append(record)
        if len(self.reset_history) > 200:
            self.reset_history = self.reset_history[-200:]
        self.restarts = 0
        # ``last_restart_at`` stays — it's a historical fact, not a
        # counter. ``restart_history`` only clears when explicitly asked.
        if clear_history:
            self.restart_history = []
        return record


def budget_exhausted(restarts: int, max_restarts: int) -> bool:
    """Has the unverified-restart counter hit the cap?

    ``max_restarts <= 0`` is the sentinel for "unlimited" — the default.
    Centralized so every caller (Monitor, doctor, vault) agrees on the
    semantics: ``0`` and any negative value mean no cap, never exhausted.
    """
    if max_restarts <= 0:
        return False
    return restarts >= max_restarts


def format_budget(max_restarts: int) -> str:
    """Render ``max_restarts`` for human-facing strings.

    ``0`` (or negative) renders as ``∞`` — the supervisor will keep
    trying without a cap. Positive values render as-is.
    """
    return "∞" if max_restarts <= 0 else str(max_restarts)


def append_reset_log(reset_log_path: Path, record: ResetRecord) -> None:
    """Append a one-line summary of ``record`` to ``reset.log``.

    Plain-text, append-only, line-buffered — designed for ``tail -F``.
    Best-effort: a write failure here never aborts the reset (the
    structured record in state.json is the source of truth; this file
    is a convenience for ops).
    """
    reset_log_path.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        record.time,
        f"source={record.source}",
        f"prior_restarts={record.prior_restarts}",
        f"prior_total={record.prior_restarts_total}",
    ]
    if record.stage:
        parts.append(f"stage={record.stage}")
    if record.cleared_history:
        parts.append("cleared_history=true")
    parts.append(f"reason={record.reason!r}")
    line = "  ".join(parts) + "\n"
    try:
        with reset_log_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


class StateStore:
    """Atomic JSON-file state store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> MonitorState:
        if not self.path.exists():
            return MonitorState()
        with self.path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return MonitorState.model_validate(data)

    def save(self, state: MonitorState) -> None:
        """Atomically write state to disk.

        Tries write-tmp + ``os.replace`` first; if the tmp vanishes
        between the two (observed on iCloud-synced directories where the
        evictor races our rename — issue #5), falls back to a direct
        non-atomic write so we degrade gracefully instead of failing
        forever. The fallback prefers correctness over atomicity: better
        to write the state than to drop it.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = state.model_dump_json(indent=2)
        with self._lock:
            try:
                with tmp.open("w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except FileNotFoundError:
                with self.path.open("w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
