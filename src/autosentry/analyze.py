"""``autosentry analyze`` — flat-text summary of the attempts ledger.

Mirrors the spirit of autoresearch's ``analysis.ipynb`` without dragging
Jupyter into the dep tree. Reads ``attempts.tsv`` (and optionally
filters by ``--since``), then prints:

- Top failing detectors in the window
- Per-rule success rate (kept / total terminal attempts)
- Current regression streaks per detector
- Overall stats (totals, in-progress)
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from autosentry.ledger import Attempt

# ----- time-window parsing -------------------------------------------------


_SINCE_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)


def parse_since(value: str | None) -> timedelta | None:
    """Parse a window specifier like ``24h``, ``30m``, ``7d``, ``600s``.

    Returns ``None`` for an empty/missing value (which means "no filter").
    Raises :class:`ValueError` on malformed input.
    """
    if not value:
        return None
    m = _SINCE_RE.match(value)
    if not m:
        msg = f"bad --since value {value!r} (expected like '24h', '30m', '7d', '600s')"
        raise ValueError(msg)
    qty = int(m.group(1))
    unit = m.group(2).lower()
    return {
        "s": timedelta(seconds=qty),
        "m": timedelta(minutes=qty),
        "h": timedelta(hours=qty),
        "d": timedelta(days=qty),
    }[unit]


def filter_window(rows: Iterable[Attempt], window: timedelta | None) -> list[Attempt]:
    if window is None:
        return list(rows)
    cutoff = datetime.now(tz=timezone.utc) - window
    out: list[Attempt] = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r.timestamp)
        except ValueError:
            # Tolerate bad timestamps — include them rather than drop them.
            out.append(r)
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            out.append(r)
    return out


# ----- summary stats -------------------------------------------------------


@dataclass(frozen=True)
class RuleStat:
    source: str
    total: int
    kept: int
    regressed: int
    crashed: int
    discarded: int
    pending: int

    @property
    def success_rate(self) -> float:
        terminal = self.kept + self.regressed + self.crashed + self.discarded
        if terminal == 0:
            return 0.0
        return self.kept / terminal


@dataclass(frozen=True)
class DetectorStat:
    detector: str
    total: int
    streak_status: str  # most recent terminal status
    streak_len: int  # consecutive run of same terminal status (most recent first)


@dataclass(frozen=True)
class Summary:
    total: int
    by_status: dict[str, int]
    top_detectors: list[tuple[str, int]]
    rules: list[RuleStat]
    detector_streaks: list[DetectorStat]


def summarize(rows: Iterable[Attempt], *, top_n: int = 10) -> Summary:
    rows = list(rows)
    # Counter[AttemptStatus] → plain dict[str, int]; the str(...) cast keeps
    # pyrefly happy and matches the Summary annotation.
    by_status: dict[str, int] = {str(k): v for k, v in Counter(r.status for r in rows).items()}
    detector_counts = Counter(r.detector for r in rows)

    # Per-rule stats.
    by_source: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        by_source[r.source][r.status] += 1
    rules = [
        RuleStat(
            source=source,
            total=sum(statuses.values()),
            kept=statuses.get("kept", 0),
            regressed=statuses.get("regressed", 0),
            crashed=statuses.get("crashed", 0),
            discarded=statuses.get("discarded", 0),
            pending=statuses.get("pending", 0),
        )
        for source, statuses in by_source.items()
    ]
    rules.sort(key=lambda r: r.total, reverse=True)

    # Detector streaks — walk backward through rows-by-detector, count the
    # longest run of identical terminal status at the tail.
    by_detector: dict[str, list[Attempt]] = defaultdict(list)
    for r in rows:
        by_detector[r.detector].append(r)
    streaks: list[DetectorStat] = []
    for detector, drows in by_detector.items():
        terminal = [r for r in drows if r.status != "pending"]
        if not terminal:
            streaks.append(
                DetectorStat(
                    detector=detector,
                    total=len(drows),
                    streak_status="pending",
                    streak_len=0,
                )
            )
            continue
        tail_status = terminal[-1].status
        streak_len = 0
        for r in reversed(terminal):
            if r.status == tail_status:
                streak_len += 1
            else:
                break
        streaks.append(
            DetectorStat(
                detector=detector,
                total=len(drows),
                streak_status=tail_status,
                streak_len=streak_len,
            )
        )
    streaks.sort(key=lambda s: s.total, reverse=True)

    return Summary(
        total=len(rows),
        by_status=by_status,
        top_detectors=detector_counts.most_common(top_n),
        rules=rules,
        detector_streaks=streaks,
    )
