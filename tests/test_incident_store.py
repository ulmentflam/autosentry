"""Incident store integration test."""

from __future__ import annotations

import json
from pathlib import Path

from autosentry.incidents.exploder import ExplodedFrame
from autosentry.incidents.store import IncidentStore, IncidentWrite


def test_write_incident_folder_layout(tmp_path: Path):
    store = IncidentStore(tmp_path / "incidents")
    cfg_snap = tmp_path / "run.yaml"
    cfg_snap.write_text("lr: 5e-5\nbatch_size: 8\n")
    w = IncidentWrite(
        kind="error",
        detector="oom",
        message="OutOfMemoryError",
        process_kind="local",
        command=["python", "train.py"],
        pid=12345,
        restart_index=1,
        max_restarts=5,
        log_excerpt=["one", "two", "three"],
        trace='Traceback (most recent call last):\n  File "x.py", line 1, in <module>\n    raise ValueError("hi")\nValueError: hi',
        frames=[
            ExplodedFrame(
                file="x.py",
                line=1,
                lang="python",
                raw='  File "x.py", line 1, in <module>',
                source=">>>  1  raise ValueError('hi')",
                enclosing="def main()",
            )
        ],
        config_snapshot_paths=[cfg_snap],
        state_snapshot={"pid": 12345, "restarts": 1},
        rule_match={"rule": "oom_rule", "source": "rule"},
        action={"kind": "restart_with_env", "set": {"BATCH_SIZE": "4"}, "notify": True},
    )
    folder = store.write(w)
    assert (folder / "report.md").exists()
    assert (folder / "trace.txt").exists()
    assert (folder / "log_excerpt.txt").exists()
    assert (folder / "frames").is_dir()
    frame_files = list((folder / "frames").iterdir())
    assert frame_files, "expected frame files"
    assert (folder / "configs" / "run.yaml").exists()
    assert (folder / "state.json").exists()
    assert (folder / "rule_match.json").exists()
    assert (folder / "fix" / "action.json").exists()

    # report.md mentions the rule and the action
    report = (folder / "report.md").read_text()
    assert "oom_rule" in report
    assert "restart_with_env" in report

    # index entry was appended
    idx = (tmp_path / "incidents" / "index.jsonl").read_text().strip().splitlines()
    assert len(idx) == 1
    entry = json.loads(idx[0])
    assert entry["id"] == folder.name
    assert entry["detector"] == "oom"
    assert entry["rule"] == "oom_rule"


def test_incidents_in_same_second_keep_distinct_evidence(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import autosentry.incidents.store as module

    fixed = datetime(2026, 9, 25, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda **_: fixed))
    store = IncidentStore(tmp_path / "incidents")
    incident = IncidentWrite(
        kind="error",
        detector="exit_code",
        message="failed",
        process_kind="local",
        command=["false"],
        pid=1,
        restart_index=0,
        max_restarts=5,
        log_excerpt=["failure"],
    )
    first = store.write(incident)
    second = store.write(incident)
    assert first != second
    assert second.name.endswith("-0001")
    assert (first / "report.md").exists() and (second / "report.md").exists()
    assert len(store.index_path.read_text().splitlines()) == 2
