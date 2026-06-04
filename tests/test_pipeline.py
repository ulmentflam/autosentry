"""Tests for the multi-stage pipeline runner.

We don't drive ``Monitor.run()`` end-to-end here — that would require
fake supervisors and a live log pipe. Instead we exercise the runner's
orchestration logic by monkey-patching ``Monitor`` with a stub whose
``run()`` returns a scripted exit code per stage. That keeps the tests
fast and lets us assert the full state transitions: pipeline.json
shape, inter-stage resets in state.json, and pipeline status after a
mid-pipeline failure.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from autosentry.config import AutoSentryConfig, ProcessConfig, StageSpec, load_config
from autosentry.pipeline import PipelineRunner, load_pipeline_state
from autosentry.state import StateStore

# ----- schema tests --------------------------------------------------------


def test_stages_and_command_are_mutually_exclusive(tmp_path: Path):
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            """\
            process:
              kind: local
              command: ["echo", "hi"]
              stages:
                - { name: a, command: ["echo", "a"] }
            """
        )
    )
    with pytest.raises(Exception, match="mutually exclusive"):
        load_config(cfg_path)


def test_pipeline_config_loads_when_only_stages_set(tmp_path: Path):
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            """\
            process:
              kind: local
              stages:
                - name: pretrain
                  command: ["python", "train.py", "--phase", "pretrain"]
                - name: sft
                  command: ["python", "train.py", "--phase", "sft"]
            """
        )
    )
    cfg = load_config(cfg_path)
    assert cfg.process.is_pipeline()
    assert [s.name for s in cfg.process.stages] == ["pretrain", "sft"]


def test_duplicate_stage_names_rejected(tmp_path: Path):
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            """\
            process:
              kind: local
              stages:
                - { name: pretrain, command: ["a"] }
                - { name: pretrain, command: ["b"] }
            """
        )
    )
    with pytest.raises(Exception, match="duplicate stage"):
        load_config(cfg_path)


def test_stage_names_must_be_path_safe(tmp_path: Path):
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            """\
            process:
              kind: local
              stages:
                - { name: "with spaces", command: ["a"] }
            """
        )
    )
    with pytest.raises(Exception, match=r"\[A-Za-z0-9_\-\]"):
        load_config(cfg_path)


# ----- runner orchestration ------------------------------------------------


@pytest.fixture
def pipeline_cfg(tmp_path: Path) -> AutoSentryConfig:
    """A three-stage pipeline config rooted at tmp_path/.autosentry/."""
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            """\
            process:
              kind: local
              cwd: "."
              env: {}
              restart_policy:
                max_restarts: 2
                cooldown_seconds: 0
              stages:
                - name: pretrain
                  command: ["python", "train.py", "--phase", "pretrain"]
                - name: sft
                  command: ["python", "train.py", "--phase", "sft"]
                - name: grpo
                  command: ["python", "train.py", "--phase", "grpo"]
            monitor:
              log_dir: ".autosentry/logs"
            state_path: ".autosentry/state.json"
            incidents_dir: ".autosentry/incidents"
            """
        )
    )
    return load_config(cfg_path)


def _patch_monitor(monkeypatch, exit_codes_by_command: dict[str, int]):
    """Replace pipeline.Monitor with a stub that doesn't spawn anything.

    The stub looks up its exit code by the joined ``cfg.process.command``
    so each stage can produce a different outcome. Tests script the
    pipeline by setting the dict; missing keys default to 0.
    """

    class _StubMonitor:
        def __init__(self, cfg) -> None:  # noqa: ANN001
            self.cfg = cfg

        def run(self) -> int:
            key = " ".join(self.cfg.process.command)
            return exit_codes_by_command.get(key, 0)

    import autosentry.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "Monitor", _StubMonitor)


def test_happy_path_advances_through_every_stage(
    tmp_path: Path, pipeline_cfg: AutoSentryConfig, monkeypatch
):
    _patch_monitor(monkeypatch, {})
    runner = PipelineRunner(pipeline_cfg)
    exit_code = runner.run()
    assert exit_code == 0

    pipeline = load_pipeline_state(tmp_path / ".autosentry" / "pipeline.json")
    assert pipeline is not None
    assert pipeline.status == "complete"
    assert pipeline.current_stage is None
    assert [s.name for s in pipeline.stages] == ["pretrain", "sft", "grpo"]
    assert {s.status for s in pipeline.stages} == {"complete"}
    for s in pipeline.stages:
        assert s.exit_code == 0
        assert s.started_at is not None and s.ended_at is not None


def test_mid_pipeline_failure_skips_remaining_stages(
    tmp_path: Path, pipeline_cfg: AutoSentryConfig, monkeypatch
):
    # SFT fails; GRPO must end up "skipped", not "pending".
    _patch_monitor(
        monkeypatch,
        {"python train.py --phase sft": 7},
    )
    runner = PipelineRunner(pipeline_cfg)
    exit_code = runner.run()
    assert exit_code == 7

    pipeline = load_pipeline_state(tmp_path / ".autosentry" / "pipeline.json")
    assert pipeline is not None
    assert pipeline.status == "failed"
    assert pipeline.current_stage is None
    statuses = {s.name: s.status for s in pipeline.stages}
    assert statuses == {"pretrain": "complete", "sft": "failed", "grpo": "skipped"}


def test_inter_stage_reset_logs_to_reset_history_and_logfile(
    tmp_path: Path, pipeline_cfg: AutoSentryConfig, monkeypatch
):
    _patch_monitor(monkeypatch, {})
    runner = PipelineRunner(pipeline_cfg)
    runner.run()

    # State should carry one ResetRecord per inter-stage transition
    # (3 stages → 2 transitions → 2 reset records). Each tagged with
    # source="pipeline" and the stage that just completed.
    state = StateStore(tmp_path / ".autosentry" / "state.json").load()
    pipeline_resets = [r for r in state.reset_history if r.source == "pipeline"]
    assert len(pipeline_resets) == 2
    assert [r.stage for r in pipeline_resets] == ["pretrain", "sft"]
    for r in pipeline_resets:
        assert "pipeline advance" in r.reason

    # reset.log mirror should have the same two lines.
    reset_log = tmp_path / ".autosentry" / "logs" / "reset.log"
    assert reset_log.exists()
    lines = reset_log.read_text().splitlines()
    assert len(lines) == 2
    assert all("source=pipeline" in line for line in lines)


def test_stage_scoped_cfg_swaps_command_and_inherits_defaults(
    pipeline_cfg: AutoSentryConfig,
):
    runner = PipelineRunner(pipeline_cfg)
    stage = pipeline_cfg.process.stages[1]  # sft
    scoped = runner._stage_scoped_cfg(stage)
    assert scoped.process.command == ["python", "train.py", "--phase", "sft"]
    # Stage didn't override these — should fall back to process-level values.
    assert scoped.process.restart_policy.max_restarts == 2
    assert scoped.process.cwd == "."
    # And stages must be cleared on the scoped copy so the inner Monitor
    # doesn't think it's still a pipeline.
    assert scoped.process.stages == []
    assert scoped.process.is_pipeline() is False


def test_stage_specific_restart_policy_overrides_process_default(tmp_path: Path):
    """Per-stage restart_policy.max_restarts beats the process-level
    default for that stage only. Validated through model construction
    rather than the YAML loader so the assertion stays focused."""
    cfg = AutoSentryConfig(
        process=ProcessConfig(
            kind="local",
            stages=[
                StageSpec(name="cheap", command=["a"]),
                StageSpec(
                    name="expensive",
                    command=["b"],
                    restart_policy={"max_restarts": 1},  # type: ignore[arg-type]
                ),
            ],
        ),
    )
    runner = PipelineRunner(cfg)
    cheap = runner._stage_scoped_cfg(cfg.process.stages[0])
    expensive = runner._stage_scoped_cfg(cfg.process.stages[1])
    # cheap inherits the process default (10 by default RestartPolicy)
    assert cheap.process.restart_policy.max_restarts == 10
    # expensive uses its own
    assert expensive.process.restart_policy.max_restarts == 1
