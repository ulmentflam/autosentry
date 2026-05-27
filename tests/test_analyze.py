"""analyze module tests — parsing, windowing, summarization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from autosentry.analyze import filter_window, parse_since, summarize
from autosentry.ledger import Attempt


def _att(**overrides) -> Attempt:
    base = {
        "timestamp": "2026-05-26T14:00:00+00:00",
        "incident_id": "i1",
        "detector": "oom",
        "source": "oom_halve_batch",
        "branch": "",
        "status": "kept",
        "duration_seconds": 0.0,
        "description": "OOM",
    }
    base.update(overrides)
    return Attempt(**base)  # type: ignore[arg-type]


# ----- parse_since ---------------------------------------------------------


def test_parse_since_units():
    assert parse_since("30s") == timedelta(seconds=30)
    assert parse_since("30m") == timedelta(minutes=30)
    assert parse_since("24h") == timedelta(hours=24)
    assert parse_since("7d") == timedelta(days=7)
    assert parse_since("") is None
    assert parse_since(None) is None


def test_parse_since_rejects_garbage():
    with pytest.raises(ValueError):
        parse_since("hello")
    with pytest.raises(ValueError):
        parse_since("24")


# ----- filter_window -------------------------------------------------------


def test_filter_window_drops_old_attempts():
    recent = _att(timestamp=datetime.now(tz=timezone.utc).isoformat())
    old = _att(timestamp=(datetime.now(tz=timezone.utc) - timedelta(days=2)).isoformat())
    rows = filter_window([recent, old], timedelta(hours=24))
    assert recent in rows
    assert old not in rows


def test_filter_window_none_returns_all():
    rows = [_att(), _att()]
    assert filter_window(rows, None) == rows


def test_filter_window_tolerates_bad_timestamps():
    bad = _att(timestamp="not a date")
    rows = filter_window([bad], timedelta(hours=1))
    # Bad timestamps are included rather than silently dropped.
    assert bad in rows


# ----- summarize -----------------------------------------------------------


def test_summarize_counts_by_status_and_detector():
    rows = [
        _att(detector="oom", source="rule_a", status="kept"),
        _att(detector="oom", source="rule_a", status="regressed"),
        _att(detector="nccl", source="rule_b", status="kept"),
        _att(detector="oom", source="claude", status="kept"),
    ]
    s = summarize(rows)
    assert s.total == 4
    assert s.by_status == {"kept": 3, "regressed": 1}
    top = dict(s.top_detectors)
    assert top["oom"] == 3
    assert top["nccl"] == 1


def test_summarize_rule_success_rate():
    rows = [
        _att(source="rule_a", status="kept"),
        _att(source="rule_a", status="kept"),
        _att(source="rule_a", status="regressed"),
        _att(source="rule_a", status="pending"),  # pending excluded from rate
    ]
    s = summarize(rows)
    rule = next(r for r in s.rules if r.source == "rule_a")
    assert rule.total == 4
    assert rule.kept == 2
    assert rule.regressed == 1
    assert rule.pending == 1
    # 2 kept out of 3 terminal = ~0.667
    assert abs(rule.success_rate - (2 / 3)) < 1e-6


def test_summarize_detector_streak_counts_consecutive_terminal_status():
    # Walk in append order: detector "training_stall" regressed twice in a row.
    rows = [
        _att(detector="training_stall", status="kept"),
        _att(detector="training_stall", status="regressed"),
        _att(detector="training_stall", status="regressed"),
    ]
    s = summarize(rows)
    stall = next(d for d in s.detector_streaks if d.detector == "training_stall")
    assert stall.streak_status == "regressed"
    assert stall.streak_len == 2


def test_summarize_empty_input():
    s = summarize([])
    assert s.total == 0
    assert s.rules == []
    assert s.top_detectors == []
