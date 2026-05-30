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
    # give-up check (``restarts >= max_restarts``) and the
    # force-Claude escalation threshold. Successful heals shouldn't
    # consume budget — new runs after a heal start with a fresh slate.
    restarts: int = 0
    # All-time counter; never resets. Audit-only.
    restarts_total: int = 0
    # Sentinel default — overridden from ``cfg.process.restart_policy``
    # on Monitor.__init__. The authoritative default lives in
    # ``config.RestartPolicy.max_restarts``.
    max_restarts: int = 10
    last_restart_at: str | None = None
    last_recovery_failed_for: str | None = None  # set when claude fix also failed
    restart_history: list[RestartRecord] = Field(default_factory=list)

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
