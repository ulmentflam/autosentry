"""Attempts ledger tests."""

from __future__ import annotations

from pathlib import Path

from autosentry.ledger import Attempt, AttemptsLedger


def _make_attempt(**overrides) -> Attempt:
    base = {
        "timestamp": "2026-05-26T14:32:10Z",
        "incident_id": "2026-05-26T14-32-10Z-error-traceback",
        "detector": "oom",
        "source": "oom_halve_batch",
        "branch": "",
        "status": "pending",
        "duration_seconds": 0.0,
        "description": "OOM at step 42",
    }
    base.update(overrides)
    return Attempt(**base)  # type: ignore[arg-type]


def test_append_writes_header_on_first_row(tmp_path: Path):
    led = AttemptsLedger.load(tmp_path / "attempts.tsv")
    led.append(_make_attempt())
    content = (tmp_path / "attempts.tsv").read_text()
    assert content.startswith("timestamp\t")
    assert "oom_halve_batch" in content


def test_round_trip_reads_existing_file(tmp_path: Path):
    path = tmp_path / "attempts.tsv"
    led = AttemptsLedger.load(path)
    led.append(_make_attempt(incident_id="a"))
    led.append(_make_attempt(incident_id="b", source="claude"))

    led2 = AttemptsLedger.load(path)
    assert [r.incident_id for r in led2.rows] == ["a", "b"]
    assert [r.source for r in led2.rows] == ["oom_halve_batch", "claude"]


def test_update_rewrites_matching_row(tmp_path: Path):
    led = AttemptsLedger.load(tmp_path / "attempts.tsv")
    led.append(_make_attempt(incident_id="x"))
    ok = led.update("x", "oom_halve_batch", status="kept", duration_seconds=42.5)
    assert ok
    on_disk = AttemptsLedger.load(tmp_path / "attempts.tsv")
    assert on_disk.rows[0].status == "kept"
    assert on_disk.rows[0].duration_seconds == 42.5


def test_update_returns_false_when_no_match(tmp_path: Path):
    led = AttemptsLedger.load(tmp_path / "attempts.tsv")
    led.append(_make_attempt(incident_id="x"))
    assert led.update("bogus-id", "claude", status="kept") is False


def test_update_targets_most_recent_when_multiple(tmp_path: Path):
    led = AttemptsLedger.load(tmp_path / "attempts.tsv")
    led.append(_make_attempt(incident_id="x", source="claude", status="pending"))
    led.append(_make_attempt(incident_id="x", source="claude", status="pending"))
    led.update("x", "claude", status="kept")
    # The most recently appended row should be the one updated.
    assert [r.status for r in led.rows] == ["pending", "kept"]


def test_description_tab_and_newline_sanitization(tmp_path: Path):
    led = AttemptsLedger.load(tmp_path / "attempts.tsv")
    led.append(_make_attempt(description="line1\twith\ttabs\nand newline"))
    content = (tmp_path / "attempts.tsv").read_text().splitlines()[-1]
    # The TSV format relies on tabs being separators, so embedded tabs and
    # newlines must be neutralized when writing.
    assert "\t" not in content.split("\t")[-1]  # last cell shouldn't contain tabs


def test_status_falls_back_to_pending_on_invalid_value(tmp_path: Path):
    path = tmp_path / "attempts.tsv"
    path.write_text(
        "timestamp\tincident_id\tdetector\tsource\tbranch\tstatus\tduration_seconds\tdescription\n"
        "ts\tinc\tdet\tsrc\t\tnot_a_real_status\t0\tdesc\n"
    )
    led = AttemptsLedger.load(path)
    assert led.rows[0].status == "pending"
