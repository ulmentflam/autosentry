"""Append-only ledger of fix attempts.

Modeled on autoresearch's ``results.tsv`` — flat TSV that's grep-able
from the shell and machine-readable for ``autosentry analyze``. One row
per fix attempt, written when the fix is applied (``pending``) and
updated to a terminal state (``kept``, ``discarded``, ``crashed``,
``regressed``) when verification completes.

Updating a row means rewriting the whole file with the row replaced.
The file should stay small enough (one row per fix attempt) that this
is fine for years.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

AttemptStatus = Literal["pending", "kept", "discarded", "crashed", "regressed"]

_HEADER = (
    "timestamp",
    "incident_id",
    "detector",
    "source",
    "branch",
    "status",
    "duration_seconds",
    "description",
)


@dataclass
class Attempt:
    timestamp: str
    incident_id: str
    detector: str
    source: str  # rule name OR 'claude'
    branch: str  # empty when no branch was created
    status: AttemptStatus
    duration_seconds: float
    description: str

    @classmethod
    def from_row(cls, row: dict[str, str]) -> Attempt:
        return cls(
            timestamp=row.get("timestamp", ""),
            incident_id=row.get("incident_id", ""),
            detector=row.get("detector", ""),
            source=row.get("source", ""),
            branch=row.get("branch", ""),
            status=_as_status(row.get("status", "pending")),
            duration_seconds=float(row.get("duration_seconds", "0") or 0),
            description=row.get("description", ""),
        )

    def to_row(self) -> str:
        # TSV — tabs are illegal in description per the autoresearch convention.
        cleaned = {k: str(v).replace("\t", " ").replace("\n", " ") for k, v in asdict(self).items()}
        return "\t".join(cleaned[k] for k in _HEADER) + "\n"


def _as_status(s: str) -> AttemptStatus:
    if s in ("pending", "kept", "discarded", "crashed", "regressed"):
        return s  # type: ignore[return-value]
    return "pending"


@dataclass
class AttemptsLedger:
    """Read/write helper for ``attempts.tsv``."""

    path: Path
    _rows: list[Attempt] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> AttemptsLedger:
        rows: list[Attempt] = []
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                header = next(f, "").strip().split("\t")
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < len(header):
                        # Tolerate ragged tails — pad with empty strings.
                        parts = parts + [""] * (len(header) - len(parts))
                    rows.append(Attempt.from_row(dict(zip(header, parts, strict=False))))
        return cls(path=path, _rows=rows)

    @property
    def rows(self) -> list[Attempt]:
        return list(self._rows)

    def append(self, attempt: Attempt) -> None:
        """Append a row; ensures header exists. ``attempts.tsv`` is
        atomic-friendly because each row is one line written with a
        single ``write`` call.
        """
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            if new_file:
                f.write("\t".join(_HEADER) + "\n")
            f.write(attempt.to_row())
        self._rows.append(attempt)

    def update(
        self,
        incident_id: str,
        source: str,
        *,
        status: AttemptStatus,
        duration_seconds: float | None = None,
    ) -> bool:
        """Rewrite the row matching (incident_id, source) with a new status.

        Matches the most recent attempt with that key. Returns ``True``
        when a row was updated; ``False`` if no match was found.
        """
        target_idx: int | None = None
        for i in range(len(self._rows) - 1, -1, -1):
            row = self._rows[i]
            if row.incident_id == incident_id and row.source == source:
                target_idx = i
                break
        if target_idx is None:
            return False
        row = self._rows[target_idx]
        self._rows[target_idx] = Attempt(
            timestamp=row.timestamp,
            incident_id=row.incident_id,
            detector=row.detector,
            source=row.source,
            branch=row.branch,
            status=status,
            duration_seconds=(
                duration_seconds if duration_seconds is not None else row.duration_seconds
            ),
            description=row.description,
        )
        self._rewrite()
        return True

    # ----- internals --------------------------------------------------------

    def _rewrite(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write("\t".join(_HEADER) + "\n")
            for row in self._rows:
                f.write(row.to_row())
        os.replace(tmp, self.path)


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
