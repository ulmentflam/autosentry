"""Run a monitor in an isolated process with a parent-owned deadline."""

from __future__ import annotations

import multiprocessing
import os
import signal
import subprocess
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection

from autosentry.config import AutoSentryConfig
from autosentry.monitor import Monitor, StageContext


@dataclass
class StageOutcome:
    exit_code: int
    reason: str
    cleanup_errors: tuple[str, ...] = ()


def _worker(cfg: AutoSentryConfig, stage: StageContext, channel: Connection) -> None:
    # Healer subprocesses inherit this group. Local workloads have their own
    # groups, reported separately so the parent can stop them when we hang.
    os.setsid()
    channel.send(("ready", str(os.getpid())))
    try:
        monitor = Monitor(cfg, stage=stage)
        monitor.supervisor.on_resource = lambda kind, identity: channel.send((kind, identity))
        code = monitor.run()
        channel.send(("result", (code, monitor.state.stop_reason or f"Monitor exited {code}")))
    except BaseException as exc:
        cfg.resolve(cfg.monitor.log_dir).mkdir(parents=True, exist_ok=True)
        (cfg.resolve(cfg.monitor.log_dir) / "worker-error.log").write_text(
            traceback.format_exc(), encoding="utf-8"
        )
        channel.send(("crash", (1, f"{type(exc).__name__}: {exc}")))
    finally:
        channel.close()


def _signal_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def _cleanup_remote(kind: str, identity: str, cfg: AutoSentryConfig) -> None:
    extra = cfg.process.extra
    if kind == "docker":
        command = extra.get("remove_command") or ["docker", "rm", "-f", "{name}"]
        command = [part.replace("{name}", identity) for part in command]
    else:
        command = extra.get("cancel_command") or ["scancel", "{job_id}"]
        command = [part.replace("{job_id}", identity) for part in command]
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        timeout=cfg.process.stage_stop_grace_seconds,
        cwd=cfg.resolve(cfg.process.cwd),
        env={**os.environ, **cfg.process.env},
    )


def run_stage(
    cfg: AutoSentryConfig,
    stage: StageContext,
    cancelled: Callable[[], int],
) -> StageOutcome:
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    worker = context.Process(target=_worker, args=(cfg, stage, writer))
    started = time.monotonic()
    deadline = cfg.process.max_stage_seconds
    result: StageOutcome | None = None
    resources: dict[str, str] = {}
    crashed = False
    worker.start()
    writer.close()

    def drain() -> None:
        nonlocal result, crashed
        while reader.poll():
            try:
                kind, value = reader.recv()
            except EOFError:
                break
            if kind in ("result", "crash"):
                result = StageOutcome(*value)
                crashed = kind == "crash"
            else:
                resources[kind] = value

    forced = False
    try:
        while worker.is_alive():
            drain()
            signum = cancelled()
            if signum:
                result = StageOutcome(128 + signum, f"Pipeline interrupted by signal {signum}")
                forced = True
                break
            if deadline and time.monotonic() - started >= deadline:
                result = StageOutcome(124, f"Stage deadline exceeded ({deadline:g} seconds)")
                forced = True
                break
            worker.join(timeout=0.05)
        if not forced:
            drain()
            if result is None:
                result = StageOutcome(
                    1, f"Stage worker exited without a result ({worker.exitcode})"
                )
                forced = True
            elif crashed:
                forced = True
    finally:
        errors: list[str] = []
        if forced or worker.is_alive():
            # Stop the monitor before cancelling its workload, so it cannot
            # interpret our cancellation as a failure to heal and restart.
            if worker.is_alive():
                worker.terminate()
            if "process_group" in resources:
                _signal_group(int(resources["process_group"]), signal.SIGTERM)
            worker.join(timeout=cfg.process.stage_stop_grace_seconds)
            # A blocked monitor cannot cooperate with SIGTERM. Kill its group
            # even if the worker already exited, to reap surviving healers.
            if "ready" in resources:
                _signal_group(int(resources["ready"]), signal.SIGKILL)
            if worker.is_alive():
                worker.kill()
            worker.join(timeout=1)
            # Startup may have completed during termination. Drain resource
            # ownership messages without replacing the timeout result.
            saved = result
            drain()
            result = saved
            if "process_group" in resources:
                _signal_group(int(resources["process_group"]), signal.SIGKILL)
            for kind in ("docker", "slurm"):
                if kind in resources:
                    try:
                        _cleanup_remote(kind, resources[kind], cfg)
                    except (OSError, subprocess.SubprocessError) as exc:
                        errors.append(f"Could not cancel {kind} {resources[kind]}: {exc}")
        reader.close()
        if not worker.is_alive():
            worker.close()
    if result is None:
        result = StageOutcome(1, "Stage worker produced no result")
    result.cleanup_errors = tuple(errors)
    return result
