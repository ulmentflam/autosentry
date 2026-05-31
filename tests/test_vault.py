"""Tests for the Obsidian-compatible vault (0.11.0).

Three layers under test:

- Vault store: file-tree creation, atomic writes, section appends.
- Pattern detection: trace-hash + message-similarity grouping with
  threshold promotion.
- Monitor integration: vault writes happen at the right lifecycle
  points and never crash the monitor when they fail.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from autosentry.config import RuleAction, load_config
from autosentry.detectors.base import Detection
from autosentry.healers.base import HealerOutcome
from autosentry.monitor import Monitor
from autosentry.vault import VaultStore
from autosentry.vault.patterns import PatternIndex

# ---------------------------------------------------------------------------
# VaultStore — direct unit tests
# ---------------------------------------------------------------------------


def test_vault_layout_created_on_init(tmp_path: Path) -> None:
    """Constructing a vault store provisions the directory tree + the
    home index note."""
    vault = VaultStore(tmp_path / "vault")
    for sub in ("runs", "incidents", "detectors", "patterns", "regressions", "exhaustions"):
        assert (vault.root / sub).is_dir()
    index = (vault.root / "index.md").read_text()
    assert "# autosentry vault" in index
    assert "kind: index" in index


def test_record_run_start_writes_note(tmp_path: Path) -> None:
    vault = VaultStore(tmp_path / "vault")
    ref = vault.record_run_start(
        run_id="run-1",
        supervisor_kind="local",
        command=["python", "app.py"],
        started_at="2026-05-31T00:00:00+00:00",
        cwd=str(tmp_path),
    )
    assert ref is not None
    text = ref.path.read_text()
    assert "# Run `run-1`" in text
    assert "kind: run" in text
    assert "python app.py" in text
    # Pre-created child-runs subdir so Obsidian shows the tree.
    assert (vault.root / "runs" / "run-1").is_dir()


def test_record_child_restart_links_back_to_run(tmp_path: Path) -> None:
    """The child-run note exists AND the parent run note picks up a
    wikilink to it under the 'Child restarts' section."""
    vault = VaultStore(tmp_path / "vault")
    vault.record_run_start(
        run_id="run-1",
        supervisor_kind="local",
        command=["true"],
        started_at="2026-05-31T00:00:00+00:00",
        cwd=".",
    )
    vault.record_child_restart(
        run_id="run-1",
        child_index=1,
        pid=12345,
        started_at="2026-05-31T00:00:01+00:00",
        reason="initial start",
    )
    child = (vault.root / "runs" / "run-1" / "child-1.md").read_text()
    assert "Child run #1 of [[run-1]]" in child
    assert "PID:** 12345" in child
    run_note = (vault.root / "runs" / "run-1.md").read_text()
    assert "[[run-1-child-1]]" in run_note
    # The placeholder *(none yet)* should have been replaced.
    assert "Child restarts" in run_note
    placeholder_idx = run_note.find("## Child restarts")
    incidents_idx = run_note.find("## Incidents")
    assert "*(none yet)*" not in run_note[placeholder_idx:incidents_idx]


def test_record_incident_updates_detector_aggregator(tmp_path: Path) -> None:
    vault = VaultStore(tmp_path / "vault")
    vault.record_run_start(
        run_id="run-1",
        supervisor_kind="local",
        command=["true"],
        started_at="2026-05-31T00:00:00+00:00",
        cwd=".",
    )
    vault.record_incident(
        incident_id="2026-05-31-error-oom",
        run_id="run-1",
        child_run_id=None,
        detector="oom",
        kind="error",
        message="OutOfMemoryError: CUDA",
        fired_at="2026-05-31T00:01:00+00:00",
        trace_hash="deadbeef",
        pattern_slug=None,
        incident_folder_rel=".autosentry/incidents/2026-05-31-error-oom",
    )
    # Per-incident note exists with the right kind + detector backlink.
    incident = (vault.root / "incidents" / "2026-05-31-error-oom.md").read_text()
    assert "kind: incident" in incident
    assert "[[detector-oom]]" in incident
    assert "deadbeef" in incident
    # Detector aggregator was created and lists the incident.
    det_note = (vault.root / "detectors" / "oom.md").read_text()
    assert "Detector `oom`" in det_note
    assert "[[2026-05-31-error-oom]]" in det_note


def test_record_attempt_resolution_updates_status(tmp_path: Path) -> None:
    vault = VaultStore(tmp_path / "vault")
    vault.record_run_start(
        run_id="run-1",
        supervisor_kind="local",
        command=["true"],
        started_at="2026-05-31T00:00:00+00:00",
        cwd=".",
    )
    vault.record_incident(
        incident_id="inc-1",
        run_id="run-1",
        child_run_id=None,
        detector="oom",
        kind="error",
        message="OOM",
        fired_at="2026-05-31T00:01:00+00:00",
        trace_hash=None,
        pattern_slug=None,
        incident_folder_rel=".autosentry/incidents/inc-1",
    )
    vault.record_attempt_start(
        incident_id="inc-1",
        attempt_index=1,
        source="rule",
        rule_name="oom-restart",
        action_kind="restart",
        action_set={},
        started_at="2026-05-31T00:01:01+00:00",
        branch=None,
    )
    vault.record_attempt_resolution(
        incident_id="inc-1",
        attempt_index=1,
        status="kept",
        ended_at="2026-05-31T00:11:01+00:00",
    )
    attempt = (vault.root / "incidents" / "inc-1" / "attempt-1.md").read_text()
    assert "status: kept" in attempt
    assert "Status:** `kept`" in attempt


# ---------------------------------------------------------------------------
# PatternIndex — promotion + trace-hash matching
# ---------------------------------------------------------------------------


def test_pattern_promotes_after_threshold_incidents(tmp_path: Path) -> None:
    idx = PatternIndex(tmp_path / "patterns.json", threshold=3, similarity=0.3)
    msg = "OutOfMemoryError: CUDA"
    first = idx.classify(incident_id="i1", detector="oom", message=msg, trace=None)
    second = idx.classify(incident_id="i2", detector="oom", message=msg, trace=None)
    third = idx.classify(incident_id="i3", detector="oom", message=msg, trace=None)
    assert first.pattern is None and not first.is_new_pattern
    assert second.pattern is None and not second.is_new_pattern
    # Third crosses the threshold.
    assert third.pattern is not None
    assert third.is_new_pattern is True
    assert third.pattern.detector == "oom"
    assert set(third.pattern.incident_ids) == {"i1", "i2", "i3"}


def test_pattern_subsequent_same_trace_hash_joins_existing(tmp_path: Path) -> None:
    """Once a pattern exists, an incident with the same trace hash
    joins it immediately — no waiting for threshold a second time."""
    idx = PatternIndex(tmp_path / "patterns.json", threshold=2)
    trace = 'File "x.py", line 10, in foo\nValueError: bad'
    idx.classify(incident_id="a", detector="tb", message="ValueError: bad", trace=trace)
    b = idx.classify(incident_id="b", detector="tb", message="ValueError: bad", trace=trace)
    assert b.pattern is not None and b.is_new_pattern is True
    # Third with same trace — joins immediately, no new pattern flag.
    c = idx.classify(incident_id="c", detector="tb", message="ValueError: bad", trace=trace)
    assert c.pattern is not None and c.is_new_pattern is False
    assert "c" in c.pattern.incident_ids


def test_pattern_index_persists_across_instances(tmp_path: Path) -> None:
    idx_path = tmp_path / "patterns.json"
    idx1 = PatternIndex(idx_path, threshold=2)
    idx1.classify(incident_id="i1", detector="oom", message="OOM", trace=None)
    idx1.classify(incident_id="i2", detector="oom", message="OOM", trace=None)
    # Reload — the promoted pattern should survive.
    idx2 = PatternIndex(idx_path, threshold=2)
    result = idx2.classify(incident_id="i3", detector="oom", message="OOM", trace=None)
    assert result.pattern is not None
    assert "i3" in result.pattern.incident_ids


# ---------------------------------------------------------------------------
# Monitor integration — vault writes happen at the right moments
# ---------------------------------------------------------------------------


def _write_cfg(tmp_path: Path, *, vault_enabled: bool = True) -> Path:
    cfg = tmp_path / "autosentry.yaml"
    cfg.write_text(
        dedent(
            f"""\
            process:
              kind: local
              command: ["true"]
              restart_policy:
                max_restarts: 2
                cooldown_seconds: 0
            monitor:
              poll_interval_seconds: 1
            detectors:
              - kind: pattern
                name: oom
                regex: "OutOfMemoryError"
            rules:
              - match: {{ detector: oom }}
                action: {{ kind: restart, notify: false }}
            healing:
              claude:
                enabled: false
            vault:
              enabled: {str(vault_enabled).lower()}
              pattern_threshold: 2
            state_path: ".autosentry/state.json"
            incidents_dir: ".autosentry/incidents"
            """
        )
    )
    return cfg


def _make_monitor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw) -> Monitor:
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path, **kw))
    monitor = Monitor(cfg)
    monitor.supervisor = MagicMock()
    monitor.supervisor.status.return_value = MagicMock(running=True, pid=42, exit_code=None)
    monitor._notify = MagicMock()
    return monitor


def test_vault_disabled_skips_all_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = _make_monitor(tmp_path, monkeypatch, vault_enabled=False)
    assert monitor.vault is None
    assert monitor.patterns is None
    # An incident write shouldn't crash even though vault is None.
    monitor._record_vault_incident(
        Detection(kind="error", detector="oom", message="m"),
        "inc-1",
        tmp_path / ".autosentry" / "incidents" / "inc-1",
        fired_at="2026-05-31T00:00:00+00:00",
    )
    assert not (tmp_path / ".autosentry" / "vault").exists()


def test_monitor_fire_detection_writes_incident_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end-lite: fire one detection and confirm the vault picks
    up the run note, child-run note, incident note, and detector
    aggregator."""
    monitor = _make_monitor(tmp_path, monkeypatch)
    # Seed the run-id / child-run-id the way ``run()`` would.
    monitor._vault_run_id = "run-test"
    monitor.vault.record_run_start(  # type: ignore[union-attr]
        run_id="run-test",
        supervisor_kind="local",
        command=["true"],
        started_at="2026-05-31T00:00:00+00:00",
        cwd=str(tmp_path),
    )
    monitor._record_vault_child_restart(
        pid=42, started_at="2026-05-31T00:00:01+00:00", reason="initial start"
    )
    # Apply the same rule action a regular fire would have produced.
    monitor.rule_healer.attempt = MagicMock(  # type: ignore[method-assign]
        return_value=HealerOutcome(
            action=RuleAction(kind="restart", notify=False), source="rule", rule_name="oom-restart"
        )
    )
    monitor.supervisor.apply_action = MagicMock()  # type: ignore[method-assign]

    det = Detection(kind="error", detector="oom", message="OutOfMemoryError: CUDA")
    monitor._fire_detection(det)

    vault_root = tmp_path / ".autosentry" / "vault"
    incident_files = list((vault_root / "incidents").glob("*.md"))
    assert incident_files, "expected an incident note"
    # Detector aggregator created + has the incident link.
    det_note = (vault_root / "detectors" / "oom.md").read_text()
    assert "[[" in det_note
    # An attempt note got written for the rule-driven action.
    attempt_files = list((vault_root / "incidents").rglob("attempt-*.md"))
    assert attempt_files, "expected an attempt sub-note"
    attempt_text = attempt_files[0].read_text()
    assert "Source:** `rule`" in attempt_text
    assert "Action:** `restart`" in attempt_text


def test_monitor_records_pattern_after_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two same-detector / same-message fires (threshold=2 in fixture)
    should promote the group to a pattern note."""
    monitor = _make_monitor(tmp_path, monkeypatch)
    monitor._vault_run_id = "run-test"
    monitor.vault.record_run_start(  # type: ignore[union-attr]
        run_id="run-test",
        supervisor_kind="local",
        command=["true"],
        started_at="2026-05-31T00:00:00+00:00",
        cwd=str(tmp_path),
    )
    monitor._record_vault_child_restart(
        pid=42, started_at="2026-05-31T00:00:01+00:00", reason="initial start"
    )
    monitor.rule_healer.attempt = MagicMock(return_value=None)  # type: ignore[method-assign]
    monitor.claude_healer.attempt = MagicMock(return_value=None)  # type: ignore[method-assign]

    import time as _time

    for _ in range(2):
        det = Detection(kind="error", detector="oom", message="OutOfMemoryError: CUDA")
        monitor._fire_detection(det)
        # Incident folder names use second-resolution timestamps;
        # space the fires so the second incident gets a distinct slug.
        _time.sleep(1.05)

    pattern_notes = list((tmp_path / ".autosentry" / "vault" / "patterns").glob("*.md"))
    assert pattern_notes, "expected a pattern note after threshold crossed"
    body = pattern_notes[0].read_text()
    assert "kind: pattern" in body
    assert "Detector:** [[detector-oom]]" in body


def test_monitor_records_exhaustion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = _make_monitor(tmp_path, monkeypatch)
    monitor._vault_run_id = "run-test"
    monitor.vault.record_run_start(  # type: ignore[union-attr]
        run_id="run-test",
        supervisor_kind="local",
        command=["true"],
        started_at="2026-05-31T00:00:00+00:00",
        cwd=str(tmp_path),
    )
    monitor.state.restarts = monitor.state.max_restarts
    monitor._record_vault_exhaustion(detector="oom")
    note = (tmp_path / ".autosentry" / "vault" / "exhaustions" / "run-test.md").read_text()
    assert "Recovery exhaustion on [[run-test]]" in note
    assert "[[detector-oom]]" in note
