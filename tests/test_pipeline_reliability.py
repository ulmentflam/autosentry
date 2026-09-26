"""Regression tests for pipeline control, recovery history, and launch checks."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autosentry.cli import app
from autosentry.config import AutoSentryConfig, ProcessConfig, StageSpec
from autosentry.diagnosis import build_bundle, render_bundle
from autosentry.monitor import Monitor
from autosentry.pipeline import PipelineRunner, load_pipeline_state
from autosentry.preflight import check_launch
from autosentry.stage_worker import StageOutcome
from autosentry.state import StateStore
from autosentry.supervisors.base import SupervisorError


def config(root: Path) -> AutoSentryConfig:
    cfg = AutoSentryConfig(
        process=ProcessConfig(
            stages=[
                StageSpec(name="first", command=[sys.executable, "-c", "print('stage one')"]),
                StageSpec(name="second", command=[sys.executable, "-c", "print('stage two')"]),
            ]
        )
    )
    cfg.config_path = root / "autosentry.yaml"
    cfg.healing.claude.enabled = False
    cfg.vault.enabled = False
    cfg.monitor.poll_interval_seconds = 0
    cfg.process.stage_stop_grace_seconds = 0.2
    return cfg


def stub_stages(monkeypatch, codes: list[int]):
    seen = []

    def execute(self, cfg, stage):
        seen.append(stage.name)
        code = codes.pop(0)
        return StageOutcome(code, f"child exit {code}")

    monkeypatch.setattr(PipelineRunner, "_run_stage", execute)
    return seen


def test_resume_preserves_prior_run_and_skips_only_completed_stages(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    seen = stub_stages(monkeypatch, [0, 7, 0])
    assert PipelineRunner(cfg).run() == 7
    old = load_pipeline_state(cfg.resolve(".autosentry/pipeline.json"))
    assert old is not None
    old_archive = cfg.resolve(f".autosentry/runs/{old.pipeline_id}/pipeline.json")
    content = old_archive.read_bytes()
    assert PipelineRunner(cfg).run(resume=True) == 0
    new = load_pipeline_state(cfg.resolve(".autosentry/pipeline.json"))
    assert new is not None and new.parent_run_id == old.pipeline_id
    assert new.stages[0].evidence_dir == old.stages[0].evidence_dir
    assert old_archive.read_bytes() == content
    assert seen == ["first", "second", "second"]


def test_resume_rejects_changed_completed_stage_without_mutating_history(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    stub_stages(monkeypatch, [0, 7])
    PipelineRunner(cfg).run()
    original = cfg.resolve(".autosentry/pipeline.json").read_bytes()
    cfg.process.stages[0].env["CHANGED"] = "yes"
    with pytest.raises(ValueError, match="Cannot skip"):
        PipelineRunner(cfg).run(resume=True)
    assert cfg.resolve(".autosentry/pipeline.json").read_bytes() == original


def test_from_stage_allows_fixing_failed_stage_but_not_skipping_failure(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    seen = stub_stages(monkeypatch, [0, 7, 0])
    PipelineRunner(cfg).run()
    cfg.process.stages[1].env["FIXED"] = "yes"
    assert PipelineRunner(cfg).run(from_stage="second") == 0
    assert seen == ["first", "second", "second"]


def test_resume_requires_verified_state(tmp_path):
    with pytest.raises(ValueError, match="No valid prior"):
        PipelineRunner(config(tmp_path)).run(resume=True)


def test_missing_executable_fails_without_starting_worker(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    cfg.process.stages[0].command = ["autosentry-nonexistent-command"]
    seen = stub_stages(monkeypatch, [])
    assert PipelineRunner(cfg).run() == 126
    assert not seen
    bundle = build_bundle(cfg)
    assert "Executable not found" in bundle["observed_stop_reason"]
    assert bundle["run"]["stages"][1]["status"] == "skipped"


def test_preflight_matches_child_relative_path_and_declared_dependencies(tmp_path):
    cfg = config(tmp_path)
    cwd = tmp_path / "work"
    binary = cwd / "bin" / "worker"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    scoped = PipelineRunner(cfg)._stage_scoped_cfg(cfg.process.stages[0])
    scoped.process.command = ["worker"]
    scoped.process.cwd = "work"
    scoped.process.env = {"PATH": "bin", "REQUIRED": "set"}
    scoped.process.required_env = ["REQUIRED"]
    assert check_launch(scoped) == []
    scoped.process.required_executables = ["missing-pixi"]
    scoped.process.env["REQUIRED"] = ""
    errors = check_launch(scoped)
    assert any("missing-pixi" in e for e in errors)
    assert any("REQUIRED" in e for e in errors)


def test_real_pipeline_workers_archive_separate_logs(tmp_path):
    cfg = config(tmp_path)
    assert PipelineRunner(cfg).run() == 0
    bundle = build_bundle(cfg)
    sources = bundle["sources"]
    logs = {key: value for key, value in sources.items() if key.endswith("process.log")}
    assert len(logs) == 2
    assert any("stage one" in text for text in logs.values())
    assert any("stage two" in text for text in logs.values())
    assert all(stage["status"] == "complete" for stage in bundle["run"]["stages"])


def blocked_worker(cfg, stage, channel):
    """A monitor that never ticks and refuses graceful termination."""
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    channel.send(("ready", str(os.getpid())))
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    channel.send(("process_group", str(child.pid)))
    cfg.resolve("owned-child.pid").write_text(str(child.pid))
    while True:
        time.sleep(1)


def test_watchdog_stops_blocked_monitor_and_its_child(tmp_path, monkeypatch):
    import autosentry.stage_worker as module

    cfg = config(tmp_path)
    cfg.process.max_stage_seconds = 2
    monkeypatch.setattr(module, "_worker", blocked_worker)
    started = time.monotonic()
    assert PipelineRunner(cfg).run() == 124
    assert time.monotonic() - started < 10
    child = int(cfg.resolve("owned-child.pid").read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("watchdog left its child alive")
    bundle = build_bundle(cfg)
    assert "Stage deadline exceeded" in bundle["observed_stop_reason"]
    assert bundle["run"]["stages"][1]["status"] == "skipped"


def test_cancellation_is_nonzero_and_does_not_advance(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    runner = PipelineRunner(cfg)
    runner._signal = signal.SIGTERM
    seen = stub_stages(monkeypatch, [])
    assert runner.run() == 143
    assert not seen


def test_restart_rate_persists_and_expires_without_resetting_on_kept_fix(tmp_path):
    cfg = config(tmp_path)
    scoped = PipelineRunner(cfg)._stage_scoped_cfg(cfg.process.stages[0])
    scoped.process.restart_policy.max_restarts_in_window = 2
    scoped.process.restart_policy.restart_window_seconds = 60
    monitor = Monitor(scoped)
    monitor._before_child_start()  # Initial launch is not a restart.
    monitor._before_child_start()
    monitor._before_child_start()
    monitor.state.started_at = datetime.now(timezone.utc).isoformat()
    monitor.state.restarts = 0  # A kept fix resets this budget only.
    monitor._save_state()
    restarted = Monitor(scoped)
    with pytest.raises(SupervisorError, match="rate limit"):
        restarted._before_child_start()
    assert restarted._final_exit_code == 1
    old = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
    restarted.state.restart_window = [old, old]
    restarted._stop = False
    restarted._before_child_start()
    assert len(restarted.state.restart_window) == 1


def test_nonrestart_action_does_not_spend_rate_budget(tmp_path):
    cfg = config(tmp_path)
    scoped = PipelineRunner(cfg)._stage_scoped_cfg(cfg.process.stages[0])
    scoped.process.restart_policy.max_restarts_in_window = 1
    monitor = Monitor(scoped)
    from autosentry.config import RuleAction

    monitor.supervisor.apply_action(RuleAction(kind="pause"))
    assert monitor.state.restart_window == []


def test_evidence_redacts_secrets_and_bounds_large_output(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    cfg.process.env["API_KEY"] = "example-secret-12345"
    stub_stages(monkeypatch, [1])
    PipelineRunner(cfg).run()
    state = load_pipeline_state(cfg.resolve(".autosentry/pipeline.json"))
    assert state is not None
    path = cfg.resolve(state.stages[0].evidence_dir) / "process.log"
    path.write_text("x" * 100_000 + "\nAPI_KEY=example-secret-12345\nreal failure\n")
    bundle = build_bundle(cfg)
    rendered = render_bundle(bundle)
    assert "example-secret-12345" not in rendered
    assert "[REDACTED]" in rendered
    assert "[Earlier content omitted]" in rendered
    assert "real failure" in rendered
    assert len(rendered) < 60_000


def test_explain_cli_emits_parseable_json(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    stub_stages(monkeypatch, [1])
    PipelineRunner(cfg).run()
    import autosentry.cli.commands.explain as module

    monkeypatch.setattr(module, "load_config", lambda _: cfg)
    result = CliRunner().invoke(app, ["explain", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["observed_stop_reason"] == "child exit 1"


def test_evidence_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError, match="Run ID"):
        build_bundle(config(tmp_path), "../../outside")


def test_real_failed_stage_has_nonzero_exit_and_cited_failure_output(tmp_path):
    cfg = config(tmp_path)
    cfg.process.lifecycle = "one_shot"
    cfg.process.stages[0].command = [
        sys.executable,
        "-c",
        "import sys; print('dataset shard missing', flush=True); sys.exit(7)",
    ]
    assert PipelineRunner(cfg).run() == 7
    bundle = build_bundle(cfg)
    assert "code 7" in bundle["observed_stop_reason"]
    assert "dataset shard missing" in render_bundle(bundle)
    state = StateStore(cfg.resolve(cfg.state_path)).load()
    assert not state.child_running
    assert state.last_exit_code == 7


def test_real_restart_loop_hits_rate_cap_and_retains_recovery_evidence(tmp_path):
    from autosentry.config import DetectorSpec, RestartPolicy

    cfg = config(tmp_path)
    cfg.process.max_stage_seconds = 10
    cfg.process.restart_policy = RestartPolicy(
        max_restarts=0,
        max_identical_failures=0,
        cooldown_seconds=0,
        max_restarts_in_window=2,
        restart_window_seconds=60,
    )
    cfg.detectors = [DetectorSpec(kind="exit_code", cooldown_seconds=0)]
    cfg.process.stages[0].command = [sys.executable, "-c", "raise SystemExit(7)"]
    assert PipelineRunner(cfg).run() == 1
    bundle = build_bundle(cfg)
    assert "Restart rate limit" in bundle["observed_stop_reason"]
    rendered = render_bundle(bundle)
    assert "restart_rate_limited" in rendered
    assert "incident" in rendered
    assert bundle["run"]["stages"][0]["final_restarts"] == 2


def test_pipeline_controller_lock_rejects_concurrent_run(tmp_path):
    import fcntl

    cfg = config(tmp_path)
    path = cfg.resolve(".autosentry/pipeline.lock")
    path.parent.mkdir(parents=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="Another pipeline"):
            PipelineRunner(cfg).run()


def test_tui_reads_archived_stage_log(tmp_path, monkeypatch):
    from autosentry.tui import gather_snapshot

    cfg = config(tmp_path)
    stub_stages(monkeypatch, [1])
    PipelineRunner(cfg).run()
    pipeline = load_pipeline_state(cfg.resolve(".autosentry/pipeline.json"))
    assert pipeline is not None
    log = cfg.resolve(pipeline.stages[0].evidence_dir) / "autosentry.log"
    log.write_text("archived stage failure\n")
    assert gather_snapshot(cfg).log_tail == ["archived stage failure"]


def test_resume_cannot_skip_failed_stage(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    stub_stages(monkeypatch, [7])
    PipelineRunner(cfg).run()
    with pytest.raises(ValueError, match="Cannot skip"):
        PipelineRunner(cfg).run(from_stage="second")


def test_external_abort_cannot_be_reported_as_success(tmp_path):
    from autosentry.inbox import apply_commands

    cfg = config(tmp_path)
    scoped = PipelineRunner(cfg)._stage_scoped_cfg(cfg.process.stages[0])
    monitor = Monitor(scoped)
    inbox = cfg.resolve(".autosentry/abort.jsonl")
    inbox.write_text(
        json.dumps({"id": "1", "user": "operator", "text": "abort", "command": "abort", "args": []})
        + "\n"
    )
    apply_commands(monitor, inbox)
    assert monitor._stop
    assert monitor._final_exit_code == 130
    assert not monitor._handle_exit(0)
    assert monitor._final_exit_code == 130


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_stage_seconds", -1),
        ("max_stage_seconds", float("inf")),
        ("stage_stop_grace_seconds", 0),
    ],
)
def test_deadline_configuration_rejects_invalid_values(field, value):
    with pytest.raises(ValueError):
        ProcessConfig(command=["true"], **{field: value})
