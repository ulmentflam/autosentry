"""Multi-stage pipeline orchestrator.

When ``process.stages`` is set, ``autosentry run`` dispatches into
:class:`PipelineRunner` instead of constructing a single
:class:`autosentry.monitor.Monitor`. The runner walks the stages in
declaration order, building a fresh Monitor per stage with a
stage-scoped copy of the config, and advances only when a stage's
child exits cleanly. Failures abort the pipeline.

Why a sibling to Monitor and not a Monitor mode:

- Each stage has its own restart budget (the supervised child for
  pretrain crashes for different reasons than SFT — sharing budget
  across stages would conflate failures).
- Each stage has its own supervisor lifetime, log buffer, vault run
  id, and incident folder context. Spinning up a fresh Monitor per
  stage is the simplest way to get those clean boundaries.
- Between stages we want a controlled "reset" (clear the unverified
  restart counter, log a ResetRecord with source="pipeline") so the
  ledger shows exactly when stage N's budget started fresh and why.

State lives in ``.autosentry/pipeline.json``. Each ``autosentry run``
on a pipeline config starts fresh — pipeline.json is overwritten with
a new ``PipelineState``. Resume-from-stage-N is a deliberately deferred
feature; if the user wants it they can ``autosentry reset`` then
manually edit pipeline.json or comment out completed stages.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from autosentry.config import AutoSentryConfig, ProcessConfig, StageSpec
from autosentry.logger import log
from autosentry.monitor import Monitor
from autosentry.state import StateStore, append_reset_log


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


StageStatus = Literal["pending", "running", "complete", "failed", "skipped"]
PipelineStatus = Literal["running", "complete", "failed"]


class StageResult(BaseModel):
    name: str
    status: StageStatus = "pending"
    started_at: str | None = None
    ended_at: str | None = None
    exit_code: int | None = None
    # Restart counter at the moment the stage ended — useful for
    # post-mortems of "did this stage burn through its budget?"
    final_restarts: int = 0


class PipelineState(BaseModel):
    """Persisted snapshot at ``.autosentry/pipeline.json``."""

    pipeline_id: str  # ISO timestamp of pipeline start; doubles as run id
    started_at: str
    ended_at: str | None = None
    status: PipelineStatus = "running"
    current_stage: str | None = None
    stages: list[StageResult] = Field(default_factory=list)


_PIPELINE_STATE_PATH = Path(".autosentry/pipeline.json")


class PipelineRunner:
    """Walks ``cfg.process.stages`` sequentially, one Monitor per stage."""

    def __init__(self, cfg: AutoSentryConfig) -> None:
        if not cfg.process.is_pipeline():
            msg = "PipelineRunner constructed without process.stages set"
            raise ValueError(msg)
        self.cfg = cfg
        self.pipeline_state_path = cfg.resolve(_PIPELINE_STATE_PATH)
        self.state_store = StateStore(cfg.resolve(cfg.state_path))
        self.reset_log_path = cfg.resolve(cfg.monitor.log_dir) / "reset.log"

    # ----- public --------------------------------------------------------

    def run(self) -> int:
        """Run the pipeline. Returns the final exit code (0 on full success)."""
        pipeline = self._initialize_pipeline_state()
        self._save_pipeline(pipeline)

        for idx, stage in enumerate(self.cfg.process.stages):
            log().info(
                f"pipeline: starting stage {idx + 1}/{len(self.cfg.process.stages)} "
                f"({stage.name!r}) — cmd={' '.join(stage.command)}"
            )
            pipeline.current_stage = stage.name
            result = pipeline.stages[idx]
            result.status = "running"
            result.started_at = _now_iso()
            self._save_pipeline(pipeline)

            # Build a stage-scoped cfg view (the Monitor reads process.command
            # / cwd / env directly; we swap in the stage's values for the
            # duration of this stage).
            stage_cfg = self._stage_scoped_cfg(stage)
            exit_code = Monitor(stage_cfg).run()

            # Capture the stage's final restart count before it gets cleared
            # for the next stage — surfaces "did we burn through the budget?"
            # in pipeline.json post-mortems without needing to dig into the
            # vault.
            current_state = self.state_store.load()
            result.final_restarts = current_state.restarts
            result.exit_code = exit_code
            result.ended_at = _now_iso()

            if exit_code == 0:
                result.status = "complete"
                log().info(f"pipeline: stage {stage.name!r} complete (exit 0)")
                # Clear the unverified-restart counter between stages so the
                # next stage starts with a clean budget. Mirror to reset.log
                # so the audit shows the inter-stage transition explicitly.
                if idx + 1 < len(self.cfg.process.stages):
                    record = current_state.record_reset(
                        reason=f"pipeline advance: {stage.name} → "
                        f"{self.cfg.process.stages[idx + 1].name}",
                        clear_history=False,
                        stage=stage.name,
                        source="pipeline",
                    )
                    self.state_store.save(current_state)
                    append_reset_log(self.reset_log_path, record)
            else:
                result.status = "failed"
                # Mark remaining stages as skipped so the json reflects what
                # didn't run, not just what failed.
                for skipped in pipeline.stages[idx + 1 :]:
                    skipped.status = "skipped"
                pipeline.status = "failed"
                pipeline.ended_at = _now_iso()
                pipeline.current_stage = None
                self._save_pipeline(pipeline)
                log().error(
                    f"pipeline: stage {stage.name!r} failed (exit {exit_code}); "
                    f"aborting — {len(self.cfg.process.stages) - idx - 1} stage(s) skipped"
                )
                return exit_code

            self._save_pipeline(pipeline)

        pipeline.status = "complete"
        pipeline.ended_at = _now_iso()
        pipeline.current_stage = None
        self._save_pipeline(pipeline)
        log().info(f"pipeline: all {len(self.cfg.process.stages)} stage(s) complete")
        return 0

    # ----- helpers -------------------------------------------------------

    def _initialize_pipeline_state(self) -> PipelineState:
        """Fresh pipeline state — overwrites any prior run.

        v1 deliberately doesn't resume mid-pipeline; every invocation is
        a clean start. Resume can be added later behind --resume without
        changing this default.
        """
        now = _now_iso()
        return PipelineState(
            pipeline_id=now,
            started_at=now,
            status="running",
            stages=[StageResult(name=s.name) for s in self.cfg.process.stages],
        )

    def _save_pipeline(self, pipeline: PipelineState) -> None:
        """Atomic write — same shape as StateStore.save (mirrors that
        store's iCloud-evictor fallback so pipelines work in the same
        cursed environments)."""
        self.pipeline_state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.pipeline_state_path.with_suffix(self.pipeline_state_path.suffix + ".tmp")
        payload = pipeline.model_dump_json(indent=2)
        try:
            with tmp.open("w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
            tmp.replace(self.pipeline_state_path)
        except FileNotFoundError:
            with self.pipeline_state_path.open("w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()

    def _stage_scoped_cfg(self, stage: StageSpec) -> AutoSentryConfig:
        """Return a copy of ``self.cfg`` with ``process`` swapped for the stage.

        - ``command`` ← stage.command (always)
        - ``cwd``     ← stage.cwd if set, else process.cwd
        - ``env``     ← process.env updated with stage.env (stage wins)
        - ``restart_policy`` ← stage value if set, else process value
        - ``lifecycle``     ← stage value if set, else process value
        - ``stages`` is cleared so the Monitor sees a single-stage cfg
          and the is_pipeline() check returns False inside the stage.
        """
        merged_env = dict(self.cfg.process.env)
        merged_env.update(stage.env)
        new_process = ProcessConfig(
            kind=self.cfg.process.kind,
            command=stage.command,
            cwd=stage.cwd or self.cfg.process.cwd,
            env=merged_env,
            restart_policy=stage.restart_policy or self.cfg.process.restart_policy,
            lifecycle=stage.lifecycle or self.cfg.process.lifecycle,
            stages=[],
            extra=dict(self.cfg.process.extra),
        )
        return self.cfg.model_copy(update={"process": new_process})


def load_pipeline_state(path: Path) -> PipelineState | None:
    """Read pipeline.json if present. Returns None when the file is
    missing or unparseable (treat as no pipeline run yet — callers
    decide whether that's an error)."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PipelineState.model_validate(data)
    except (OSError, ValueError):
        return None
