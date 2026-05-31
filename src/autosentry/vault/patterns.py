"""Pattern detection: when does an incident belong to a recurring
failure mode?

Two heuristics, applied in order:

1. **Trace fingerprint** — SHA-256 of the *normalized* stack trace.
   Two incidents with the same trace (modulo line numbers, file path
   prefixes, and ephemeral values like PIDs / addresses) share a
   trace_hash. Any incident with a non-empty trace gets a hash;
   the first hash wins the slug.

2. **Message similarity** — same detector + Levenshtein distance
   under ``similarity_threshold * max(len(a), len(b))``. Lets us
   group incidents that don't carry a trace (e.g. stall detectors).

The :class:`PatternIndex` is the in-memory state the monitor consults
on each new incident: ``classify(detection)`` returns the pattern slug
(creating one if the threshold is met) and the list of incident ids
that share it.

Persisted to ``.autosentry/vault/.patterns.json`` so the index
survives monitor restarts. JSON read on init; written atomically on
every update — small file, low write rate, no need for anything
fancier.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from autosentry.logger import log


@dataclass
class PatternEntry:
    """A recurring failure mode grouped by trace hash or message similarity."""

    slug: str
    detector: str
    representative_message: str
    trace_hash: str | None
    incident_ids: list[str] = field(default_factory=list)


@dataclass
class ClassifyResult:
    """Outcome of classifying a new incident.

    - ``pattern`` is None when the incident is the first of its kind
      and the pattern threshold isn't met yet (an incident-to-pattern
      "pending" slot is still tracked internally so subsequent matches
      eventually promote it).
    - ``is_new_pattern`` is True only on the call that crosses the
      threshold — that's the trigger for an LLM narrative in 0.11.1.
    """

    pattern: PatternEntry | None
    is_new_pattern: bool
    trace_hash: str | None
    pattern_slug_for_incident: str | None


class PatternIndex:
    """In-memory + on-disk index of recurring failure modes."""

    def __init__(self, path: Path, *, threshold: int = 3, similarity: float = 0.3) -> None:
        self.path = path
        self.threshold = threshold
        self.similarity = similarity
        self._lock = threading.Lock()
        self._patterns: dict[str, PatternEntry] = {}
        # Pre-threshold groups: detector-name -> list of (incident_id,
        # message, trace_hash). When a group's len reaches ``threshold``
        # it gets promoted to a PatternEntry.
        self._pending: dict[str, list[tuple[str, str, str | None]]] = {}
        # trace_hash -> pattern slug; populated when a pattern is
        # created so subsequent same-hash incidents jump straight to
        # the existing pattern.
        self._by_trace_hash: dict[str, str] = {}
        self._load()

    # ----- persistence ------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log().error(f"vault: pattern index unreadable ({e}); starting fresh")
            return
        for slug, p in (data.get("patterns") or {}).items():
            entry = PatternEntry(
                slug=slug,
                detector=p["detector"],
                representative_message=p["representative_message"],
                trace_hash=p.get("trace_hash"),
                incident_ids=list(p.get("incident_ids") or []),
            )
            self._patterns[slug] = entry
            if entry.trace_hash:
                self._by_trace_hash[entry.trace_hash] = slug
        self._pending = {
            det: [tuple(item) for item in items]  # type: ignore[misc]
            for det, items in (data.get("pending") or {}).items()
        }

    def _save(self) -> None:
        payload = {
            "patterns": {
                slug: {
                    "detector": p.detector,
                    "representative_message": p.representative_message,
                    "trace_hash": p.trace_hash,
                    "incident_ids": p.incident_ids,
                }
                for slug, p in self._patterns.items()
            },
            "pending": {
                det: [list(item) for item in items] for det, items in self._pending.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                f.write(json.dumps(payload, indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except FileNotFoundError:
            with self.path.open("w", encoding="utf-8") as f:
                f.write(json.dumps(payload, indent=2))

    # ----- public API -------------------------------------------------------

    def classify(
        self,
        *,
        incident_id: str,
        detector: str,
        message: str,
        trace: str | None,
    ) -> ClassifyResult:
        """Decide which pattern (if any) this incident belongs to.

        Three outcomes:

        1. Matches an existing pattern (by trace hash or message
           similarity within the detector). Append to it; return
           ``is_new_pattern=False`` and the existing pattern.
        2. Pre-threshold group: less than ``threshold`` similar
           incidents recorded; track it and return ``pattern=None``.
        3. Threshold crossed: promote the pending group to a
           PatternEntry, return it with ``is_new_pattern=True``.
        """
        trace_hash = _normalize_and_hash(trace) if trace else None
        with self._lock:
            # 1) trace-hash match wins outright.
            if trace_hash and trace_hash in self._by_trace_hash:
                slug = self._by_trace_hash[trace_hash]
                p = self._patterns[slug]
                p.incident_ids.append(incident_id)
                self._save()
                return ClassifyResult(
                    pattern=p,
                    is_new_pattern=False,
                    trace_hash=trace_hash,
                    pattern_slug_for_incident=slug,
                )
            # 2) message-similarity match against existing patterns.
            for slug, p in self._patterns.items():
                if p.detector != detector:
                    continue
                if _similar(p.representative_message, message, self.similarity):
                    p.incident_ids.append(incident_id)
                    if trace_hash and not p.trace_hash:
                        p.trace_hash = trace_hash
                        self._by_trace_hash[trace_hash] = slug
                    self._save()
                    return ClassifyResult(
                        pattern=p,
                        is_new_pattern=False,
                        trace_hash=trace_hash,
                        pattern_slug_for_incident=slug,
                    )
            # 3) pending group bookkeeping.
            bucket = self._pending.setdefault(detector, [])
            # Merge into an existing pending entry if similar.
            for existing in bucket:
                eid_prev, msg_prev, _hash_prev = existing
                if _similar(msg_prev, message, self.similarity):
                    bucket.append((incident_id, message, trace_hash))
                    if len(bucket) >= self.threshold:
                        return self._promote(detector, message, trace_hash)
                    self._save()
                    return ClassifyResult(
                        pattern=None,
                        is_new_pattern=False,
                        trace_hash=trace_hash,
                        pattern_slug_for_incident=None,
                    )
            # New singleton in the pending bucket.
            bucket.append((incident_id, message, trace_hash))
            if len(bucket) >= self.threshold:
                return self._promote(detector, message, trace_hash)
            self._save()
            return ClassifyResult(
                pattern=None,
                is_new_pattern=False,
                trace_hash=trace_hash,
                pattern_slug_for_incident=None,
            )

    def _promote(self, detector: str, message: str, trace_hash: str | None) -> ClassifyResult:
        """Convert the matching pending group into a PatternEntry.

        Called under ``self._lock``. The group is identified by
        ``detector`` and (transitively) ``message`` since promotion
        only happens when a similarity match within the pending bucket
        is found.
        """
        bucket = self._pending.get(detector, [])
        # Pick the items that are similar to ``message``; that's our
        # cohort. Non-matching items stay in the pending bucket as
        # their own group (rare but possible — different fault classes
        # share a detector name).
        cohort = [item for item in bucket if _similar(item[1], message, self.similarity)]
        leftovers = [item for item in bucket if item not in cohort]
        if not cohort:
            # Shouldn't happen — caller has at least one similar item.
            cohort = bucket[:]
            leftovers = []
        slug = _slug_from_message(detector, message)
        # Resolve slug collisions deterministically by appending a
        # short hash. Same input -> same slug; safe for retries.
        if slug in self._patterns:
            slug = f"{slug}-{hashlib.sha256((detector + message).encode()).hexdigest()[:6]}"
        entry = PatternEntry(
            slug=slug,
            detector=detector,
            representative_message=message,
            trace_hash=trace_hash or next((it[2] for it in cohort if it[2]), None),
            incident_ids=[it[0] for it in cohort],
        )
        self._patterns[slug] = entry
        if entry.trace_hash:
            self._by_trace_hash[entry.trace_hash] = slug
        if leftovers:
            self._pending[detector] = leftovers
        else:
            self._pending.pop(detector, None)
        self._save()
        return ClassifyResult(
            pattern=entry,
            is_new_pattern=True,
            trace_hash=trace_hash,
            pattern_slug_for_incident=slug,
        )


# ---------------------------------------------------------------------------
# Normalization + hashing
# ---------------------------------------------------------------------------


_LINE_NO_RE = re.compile(r"line \d+")
_HEX_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_PATH_PREFIX_RE = re.compile(r'(/[^"\'\s]+/)+')
_PID_RE = re.compile(r"pid[=: ]\d+", re.IGNORECASE)
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?")


def _normalize_trace(trace: str) -> str:
    """Strip ephemeral artifacts (line numbers, hex addresses, path
    prefixes, pids, timestamps) so two crashes from the same bug
    hash identically even if they fired from different runs.

    Conservative — we'd rather two near-identical traces hash apart
    than collide unrelated failures.
    """
    t = trace
    t = _LINE_NO_RE.sub("line N", t)
    t = _HEX_ADDR_RE.sub("0xADDR", t)
    t = _PATH_PREFIX_RE.sub("/", t)
    t = _PID_RE.sub("pid=N", t)
    t = _TIMESTAMP_RE.sub("TIMESTAMP", t)
    # Collapse runs of whitespace so trailing-space differences don't
    # split the hash.
    return " ".join(t.split())


def _normalize_and_hash(trace: str) -> str:
    return hashlib.sha256(_normalize_trace(trace).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def _similar(a: str, b: str, threshold: float) -> bool:
    """``True`` when normalized Levenshtein distance is within
    ``threshold * max(len(a), len(b))``.

    Same normalization as the trace hash so timestamps / pids /
    addresses don't blow up the distance.
    """
    if a == b:
        return True
    na = _normalize_message(a)
    nb = _normalize_message(b)
    if na == nb:
        return True
    longer = max(len(na), len(nb))
    if longer == 0:
        return True
    distance = _levenshtein(na, nb)
    return distance <= threshold * longer


def _normalize_message(s: str) -> str:
    s = _HEX_ADDR_RE.sub("0xADDR", s)
    s = _PID_RE.sub("pid=N", s)
    s = _TIMESTAMP_RE.sub("TIMESTAMP", s)
    s = re.sub(r"\b\d+\b", "N", s)  # collapse standalone integers
    return " ".join(s.split())


def _levenshtein(a: str, b: str) -> int:
    """Classic DP Levenshtein. Inputs are short detection messages
    (typically < 240 chars), so the O(len(a)*len(b)) cost is fine."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                curr[j - 1] + 1,  # insertion
                prev[j] + 1,  # deletion
                prev[j - 1] + cost,  # substitution
            )
        prev = curr
    return prev[-1]


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------


_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def _slug_from_message(detector: str, message: str) -> str:
    """Build a short, stable, human-readable slug for a pattern.

    Format: ``<detector>-<3-4 keywords from message>``. Collisions
    are resolved by the caller via a hash suffix.
    """
    cleaned = _SLUG_STRIP_RE.sub("-", _normalize_message(message).lower()).strip("-")
    parts = [p for p in cleaned.split("-") if len(p) > 1 and not p.isdigit()][:4]
    if not parts:
        parts = ["unknown"]
    return f"{detector}-" + "-".join(parts)
