"""Sequential pipelines with durable run history and independent stage deadlines."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from autosentry.config import AutoSentryConfig, ProcessConfig, StageSpec
from autosentry.journal import Journal
from autosentry.monitor import StageContext
from autosentry.preflight import check_launch
from autosentry.stage_worker import StageOutcome, run_stage
from autosentry.state import StateStore, append_reset_log


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


StageStatus = Literal["pending", "running", "complete", "failed", "skipped"]
PipelineStatus = Literal["running", "complete", "failed"]


class StageResult(BaseModel):
    name: str
    status: StageStatus = "pending"
    started_at: str | None = None
    ended_at: str | None = None
    exit_code: int | None = None
    final_restarts: int = 0
    reason: str | None = None
    fingerprint: str | None = None
    evidence_dir: str | None = None
    cleanup_errors: list[str] = Field(default_factory=list)


class PipelineState(BaseModel):
    schema_version: int = 2
    pipeline_id: str
    parent_run_id: str | None = None
    started_at: str
    ended_at: str | None = None
    status: PipelineStatus = "running"
    current_stage: str | None = None
    stages: list[StageResult] = Field(default_factory=list)


class PipelineRunner:
    def __init__(self, cfg: AutoSentryConfig) -> None:
        if not cfg.process.is_pipeline():
            raise ValueError("PipelineRunner requires process.stages")
        self.cfg = cfg
        self.pipeline_state_path = cfg.resolve(".autosentry/pipeline.json")
        self.state_store = StateStore(cfg.resolve(cfg.state_path))
        self.reset_log_path = cfg.resolve(cfg.monitor.log_dir) / "reset.log"
        self._signal = 0

    def run(self, *, resume: bool = False, from_stage: str | None = None) -> int:
        if resume and from_stage:
            raise ValueError("Use either --resume or --from-stage, not both")
        self.pipeline_state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.pipeline_state_path.with_suffix(".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Another pipeline controller is running in this project") from exc
            handlers = {}
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    handlers[signum] = signal.signal(signum, self._cancel)
            try:
                return self._run(resume=resume, from_stage=from_stage)
            finally:
                for signum, handler in handlers.items():
                    signal.signal(signum, handler)

    def _cancel(self, signum: int, _frame: object) -> None:
        self._signal = signum

    def _fingerprint(self, stage: StageSpec) -> str:
        payload = json.dumps(self._stage_scoped_cfg(stage).model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def _initialize_pipeline_state(
        self,
        *,
        resume: bool = False,
        from_stage: str | None = None,
    ) -> PipelineState:
        previous = load_pipeline_state(self.pipeline_state_path)
        stages = [
            StageResult(name=s.name, fingerprint=self._fingerprint(s))
            for s in self.cfg.process.stages
        ]
        start = 0
        if resume or from_stage:
            if previous is None:
                raise ValueError("No valid prior pipeline run is available to resume")
            if [s.name for s in previous.stages] != [s.name for s in stages]:
                raise ValueError("Pipeline stage names or order changed; start a fresh run")
            if from_stage:
                names = [s.name for s in stages]
                if from_stage not in names:
                    raise ValueError(f"Unknown stage: {from_stage}")
                start = names.index(from_stage)
            else:
                start = next(
                    (i for i, s in enumerate(previous.stages) if s.status != "complete"),
                    len(stages),
                )
            for idx in range(start):
                old = previous.stages[idx]
                if old.status != "complete" or old.fingerprint != stages[idx].fingerprint:
                    raise ValueError(
                        f"Cannot skip stage {old.name!r}: completion or configuration is unverified"
                    )
                stages[idx] = old.model_copy(deep=True)
        return PipelineState(
            pipeline_id=uuid4().hex,
            started_at=_now_iso(),
            stages=stages,
            parent_run_id=previous.pipeline_id if previous and (resume or from_stage) else None,
        )

    def _run(self, *, resume: bool, from_stage: str | None) -> int:
        pipeline = self._initialize_pipeline_state(resume=resume, from_stage=from_stage)
        run_dir = self.cfg.resolve(".autosentry/runs") / pipeline.pipeline_id
        journal = Journal(run_dir / "events.jsonl", run_id=pipeline.pipeline_id)
        journal.write("pipeline_started", parent_run_id=pipeline.parent_run_id)
        self._save_pipeline(pipeline)
        for idx, stage in enumerate(self.cfg.process.stages):
            result = pipeline.stages[idx]
            if result.status == "complete":
                journal.write("stage_reused", stage=stage.name, evidence_dir=result.evidence_dir)
                continue
            pipeline.current_stage = stage.name
            result.status = "running"
            result.started_at = _now_iso()
            evidence = run_dir / "stages" / stage.name
            evidence.mkdir(parents=True, exist_ok=True)
            result.evidence_dir = str(evidence.relative_to(self.cfg.resolve(".")))
            scoped = self._stage_scoped_cfg(stage)
            scoped.monitor = scoped.monitor.model_copy(update={"log_dir": str(evidence)})
            state = self.state_store.load()
            prior_restarts = state.restarts
            if idx == 0 or resume or from_stage:
                # Fresh attempts never inherit an exhausted stage budget.
                state.restarts = 0
                state.restart_window = []
                state.started_at = None
                self.state_store.save(state)
            state.stage = stage.name
            state.stage_index = idx + 1
            state.stage_count = len(pipeline.stages)
            state.stop_reason = None
            state.pid = None
            state.child_running = False
            state.child_started_at = None
            state.child_dead_since = None
            self.state_store.save(state)
            self._save_pipeline(pipeline)
            journal.write(
                "stage_started",
                stage=stage.name,
                evidence_dir=result.evidence_dir,
                max_stage_seconds=scoped.process.max_stage_seconds,
                prior_restarts=prior_restarts,
            )
            errors = check_launch(scoped)
            (evidence / "launch.json").write_text(
                json.dumps(
                    {
                        "command": scoped.process.command,
                        "cwd": str(scoped.resolve(scoped.process.cwd)),
                        "environment_keys": sorted(scoped.process.env),
                        "required_executables": scoped.process.required_executables,
                        "required_env": scoped.process.required_env,
                        "restart_policy": scoped.process.restart_policy.model_dump(),
                        "detectors": [det.model_dump() for det in scoped.detectors],
                        "lifecycle": scoped.process.lifecycle,
                        "healer_mode": scoped.healing.claude.mode,
                        "healer_enabled": scoped.healing.claude.enabled,
                        "max_stage_seconds": scoped.process.max_stage_seconds,
                        "preflight_errors": errors,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            journal.write("preflight", stage=stage.name, errors=errors)
            if self._signal:
                outcome = StageOutcome(128 + self._signal, "Pipeline interrupted before launch")
            elif errors:
                outcome = StageOutcome(126, "; ".join(errors))
            else:
                try:
                    outcome = self._run_stage(
                        scoped,
                        StageContext(
                            name=stage.name,
                            index=idx + 1,
                            count=len(pipeline.stages),
                            run_id=pipeline.pipeline_id,
                        ),
                    )
                except Exception as exc:
                    outcome = StageOutcome(
                        1, f"Stage controller error: {type(exc).__name__}: {exc}"
                    )
            state = self.state_store.load()
            result.final_restarts = state.restarts
            result.exit_code = outcome.exit_code
            result.reason = outcome.reason
            result.cleanup_errors = list(outcome.cleanup_errors)
            result.ended_at = _now_iso()
            result.status = "complete" if outcome.exit_code == 0 else "failed"
            state.last_exit_code = outcome.exit_code
            state.stop_reason = outcome.reason
            if scoped.process.kind != "attach" and not outcome.cleanup_errors:
                state.child_running = False
                state.child_dead_since = result.ended_at
            self.state_store.save(state)
            (evidence / "state.json").write_text(state.model_dump_json(indent=2), encoding="utf-8")
            ledger = self.cfg.resolve(".autosentry/attempts.tsv")
            if ledger.exists():
                (evidence / "attempts.tsv").write_bytes(ledger.read_bytes())
            journal.write(
                "stage_finished",
                stage=stage.name,
                exit_code=outcome.exit_code,
                reason=outcome.reason,
                cleanup_errors=outcome.cleanup_errors,
            )
            if outcome.exit_code:
                for remaining in pipeline.stages[idx + 1 :]:
                    remaining.status = "skipped"
                pipeline.status = "failed"
                return self._finish(pipeline, journal, outcome.exit_code)
            if idx + 1 < len(pipeline.stages):
                record = state.record_reset(
                    f"pipeline advance: {stage.name} -> {pipeline.stages[idx + 1].name}",
                    source="pipeline",
                    stage=stage.name,
                )
                state.started_at = None
                self.state_store.save(state)
                append_reset_log(self.reset_log_path, record)
            self._save_pipeline(pipeline)
        pipeline.status = "complete"
        return self._finish(pipeline, journal, 0)

    def _run_stage(self, cfg: AutoSentryConfig, stage: StageContext) -> StageOutcome:
        return run_stage(cfg, stage, lambda: self._signal)

    def _finish(self, pipeline: PipelineState, journal: Journal, code: int) -> int:
        pipeline.current_stage = None
        pipeline.ended_at = _now_iso()
        self._save_pipeline(pipeline)
        journal.write("pipeline_finished", status=pipeline.status, exit_code=code)
        state = self.state_store.load()
        state.stage = None
        state.stage_index = None
        state.stage_count = None
        state.last_exit_code = code
        failed = next((stage for stage in pipeline.stages if stage.status == "failed"), None)
        state.stop_reason = failed.reason if failed else "Pipeline completed successfully"
        if self.cfg.process.kind != "attach" and not (failed and failed.cleanup_errors):
            state.child_running = False
            state.child_dead_since = pipeline.ended_at
        self.state_store.save(state)
        from autosentry.diagnosis import build_bundle, render_bundle

        run_dir = self.cfg.resolve(".autosentry/runs") / pipeline.pipeline_id
        (run_dir / "diagnosis.md").write_text(
            render_bundle(build_bundle(self.cfg, pipeline.pipeline_id)), encoding="utf-8"
        )
        return code

    def _save_pipeline(self, pipeline: PipelineState) -> None:
        archive = self.cfg.resolve(".autosentry/runs") / pipeline.pipeline_id / "pipeline.json"
        for path in (archive, self.pipeline_state_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(pipeline.model_dump_json(indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)

    def _stage_scoped_cfg(self, stage: StageSpec) -> AutoSentryConfig:
        process = self.cfg.process
        scoped = ProcessConfig(
            kind=process.kind,
            command=stage.command,
            cwd=stage.cwd or process.cwd,
            env={**process.env, **stage.env},
            restart_policy=stage.restart_policy or process.restart_policy,
            lifecycle=stage.lifecycle or process.lifecycle,
            extra=dict(process.extra),
            max_stage_seconds=(
                stage.max_stage_seconds
                if stage.max_stage_seconds is not None
                else process.max_stage_seconds
            ),
            stage_stop_grace_seconds=process.stage_stop_grace_seconds,
            required_executables=process.required_executables + stage.required_executables,
            required_env=process.required_env + stage.required_env,
        )
        return self.cfg.model_copy(update={"process": scoped})


def load_pipeline_state(path: Path) -> PipelineState | None:
    try:
        return PipelineState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
