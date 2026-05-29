"""Monitor main loop.

Coordinates supervisor, detectors, healers, incident store, and
notifiers. The loop is intentionally simple and synchronous: one
thread of control, log lines pulled off the supervisor's queue,
detectors run on each line, detections fan out to healers, the
winning healer's action is applied via the supervisor, and the whole
event becomes an incident folder + a notification.
"""

from __future__ import annotations

import signal
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from autosentry import git_ops
from autosentry.config import AutoSentryConfig
from autosentry.detectors import Detection, build_detectors
from autosentry.git_ops import FixBranch
from autosentry.healers import ClaudeHealer, RuleHealer
from autosentry.inbox import apply_commands as apply_inbox_commands
from autosentry.incidents import IncidentStore, IncidentWrite, SourceExploder
from autosentry.ledger import Attempt, AttemptsLedger, now_iso
from autosentry.logger import SentryLogger, log
from autosentry.notifiers import build_notifiers
from autosentry.notifiers.base import Notification
from autosentry.state import MonitorState, StateStore
from autosentry.supervisors import supervisor_for
from autosentry.supervisors.base import LogLine

# Default paths for the dispatcher's coordination files. The monitor reads
# the inbox each tick and touches the marker on each detection fire — that's
# the entire contract with the dispatcher daemon.
_INBOX_PATH = Path(".autosentry/slack_inbox.jsonl")
_INBOUND_MARKER_PATH = Path(".autosentry/inbox_poll_request")
_ATTEMPTS_PATH = Path(".autosentry/attempts.tsv")


class Monitor:
    def __init__(self, cfg: AutoSentryConfig) -> None:
        self.cfg = cfg
        SentryLogger.configure(
            log_path=cfg.resolve(cfg.monitor.log_dir) / "autosentry.log",
            also_stdout=True,
        )
        self.state_store = StateStore(cfg.resolve(cfg.state_path))
        self.state: MonitorState = self.state_store.load()
        self.state.max_restarts = cfg.process.restart_policy.max_restarts

        self.supervisor = supervisor_for(cfg, log_dir=cfg.resolve(cfg.monitor.log_dir))
        self.detectors = build_detectors(cfg.detectors)
        self.rule_healer = RuleHealer(cfg.rules)
        self.claude_healer = ClaudeHealer(cfg)
        self.exploder = SourceExploder(
            context_lines=cfg.source_explode.context_lines,
            skip_paths=cfg.source_explode.skip_paths,
            max_frames=cfg.source_explode.max_frames,
            enabled_languages=cfg.source_explode.languages,
        )
        self.incident_store = IncidentStore(cfg.resolve(cfg.incidents_dir))
        self.notifiers = build_notifiers(cfg.notifiers)
        self.last_incident_dir: Path | None = None
        self._stop = False
        self._log_buf: deque[str] = deque(maxlen=cfg.monitor.log_excerpt_lines)
        # Coordination paths with the dispatcher daemon. Both default to a
        # relative path under cfg's base dir so users only need to override if
        # their dispatcher is configured differently.
        self.inbox_path = cfg.resolve(_INBOX_PATH)
        self.inbound_marker_path = cfg.resolve(_INBOUND_MARKER_PATH)
        # Attempts ledger + outcome verification state.
        self.attempts = AttemptsLedger.load(cfg.resolve(_ATTEMPTS_PATH))
        # Pending verifications: detector → (deadline_monotonic, incident_id, source, branch).
        self._pending_verify: dict[str, tuple[float, str, str, FixBranch | None]] = {}
        # Rolling per-detector attempt timestamps for budget enforcement.
        self._recent_attempts: dict[str, list[float]] = {}
        # When a budget burns through, remember which detectors are paused.
        self._budget_paused: set[str] = set()
        # Force-Claude escalation. Flipped on when state.restarts reaches
        # the threshold; flipped off on the next kept verification. While
        # set, the next detection skips the rule healer and goes straight
        # to Claude — get the heavier diagnosis a shot before we burn the
        # rest of the budget on restarts that aren't sticking.
        self._escalation_active: bool = False
        self._escalation_threshold: int = self._resolve_escalation_threshold()
        # When a rule-based fix regresses we want the *next* attempt to
        # skip rules and go straight to Claude — rules already failed
        # on this detector, so cycling them again is wasted work.
        self._force_claude_next: set[str] = set()

    def _resolve_escalation_threshold(self) -> int:
        explicit = self.cfg.healing.escalate_to_claude_after
        if explicit is not None and explicit > 0:
            return explicit
        # Default: a fifth of the budget, rounded down (with a floor of
        # 1). With max_restarts=10 that's 2 unverified restarts. The
        # agentic flow is the main fix path — rules get a couple of
        # cheap shots at known transients, then Claude takes over.
        return max(1, self.state.max_restarts // 5)

    # ----- public lifecycle -------------------------------------------------

    def run(self) -> None:
        # Only install signal handlers when invoked from the main thread
        # (signal.signal raises ValueError otherwise — e.g. from a test).
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)

        log().info(
            f"autosentry starting — supervisor={self.cfg.process.kind} "
            f"cmd={' '.join(self.cfg.process.command)}"
        )
        self._notify("start", "autosentry monitor starting", " ".join(self.cfg.process.command))

        status = self.supervisor.start()
        self.state.pid = status.pid
        self.state.started_at = status.started_at
        self._save_state()

        line_iter = self.supervisor.iter_log_lines()
        last_tick = time.monotonic()
        try:
            while not self._stop:
                # Drain available lines (bounded so we still run housekeeping).
                drained = 0
                while drained < 200 and not self._stop:
                    try:
                        item = next(line_iter)
                    except StopIteration:
                        item = None
                        # Process has ended; let observe_status handle exit.
                        break
                    if item is None:
                        break
                    self._handle_line(item)
                    drained += 1

                now = time.monotonic()
                if now - last_tick >= self.cfg.monitor.poll_interval_seconds or drained == 0:
                    self._tick()
                    # Drain any human commands queued by the dispatcher.
                    # Cheap local file read; lives next to the anomaly-
                    # detection cycle.
                    self._consume_inbox()
                    # Resolve any pending fix verifications whose window has
                    # elapsed (the "fix worked" path; regressions are handled
                    # in the detection fire path).
                    self._tick_verifications()
                    last_tick = now

                status = self.supervisor.status()
                if not status.running and self.state.last_exit_code != status.exit_code:
                    self.state.last_exit_code = status.exit_code
                    self._save_state()
                    if not self._handle_exit(status.exit_code or 0):
                        break

                self.state.last_heartbeat = _now_iso()
                self._save_state()
        finally:
            self.supervisor.stop()
            self._notify("exit", "autosentry monitor stopping", "")
            log().info("autosentry stopped")

    # ----- inner loop -------------------------------------------------------

    def _handle_signal(self, *_args) -> None:  # type: ignore[no-untyped-def]
        log().info("received termination signal — shutting down")
        self._stop = True

    def _handle_line(self, line: LogLine) -> None:
        self._log_buf.append(line.text)
        for det in self.detectors:
            d = det.observe_line(line)
            if d is not None:
                self._fire_detection(d)

    def _tick(self) -> None:
        status = self.supervisor.status()
        for det in self.detectors:
            d = det.observe_status(status)
            if d is not None:
                self._fire_detection(d)
            d = det.observe_tick()
            if d is not None:
                self._fire_detection(d)

    def _handle_exit(self, exit_code: int) -> bool:
        """Return True to continue (after restart), False to stop the monitor."""
        log().info(f"process exited with code {exit_code}")
        # exit_code detector picks this up too, but we may need to call it explicitly
        # to give a detection if the user didn't configure one. Let detectors fire on
        # status; the actual restart decision goes through the standard healer path.
        # If no healer fires (and we're below max_restarts), default behavior is to
        # keep watching. If process is dead and there are no rules, leave it.
        if self.state.restarts >= self.state.max_restarts:
            log().error(f"max restarts ({self.state.max_restarts}) reached — giving up")
            self._notify(
                "exit",
                "max restarts reached",
                f"giving up after {self.state.restarts} restart(s)",
            )
            return False
        # The exit_code detector will have already fired during the tick above. If
        # no recovery happened, give the user a chance to manually intervene.
        time.sleep(self.cfg.process.restart_policy.cooldown_seconds)
        return True

    def _restart_policy_fallback(self, det: Detection) -> None:
        """Last-resort recovery: child is dead and no healer applied an action.

        Without this, the supervisor wheel-spun over a dead child waiting for
        a human to intervene — pegging a CPU core, never restarting, never
        exiting, while ``ps`` showed the supervisor "alive" (issue #4).

        We exercise the configured ``restart_policy`` budget the way users
        expect from ``max_restarts``: restart the child up to that many
        times with ``cooldown_seconds`` between attempts, then stop the
        monitor so a service manager can decide what to do with the
        unhealthy supervisor. Only triggered from the no-action path in
        :meth:`_fire_detection` when the supervisor isn't running.
        """
        if self._stop:
            return
        if self.state.restarts >= self.state.max_restarts:
            log().error(
                f"no recovery applied for {det.detector!r} and max restarts "
                f"({self.state.max_restarts}) reached — stopping monitor"
            )
            self._notify(
                "exit",
                "recovery exhausted",
                f"max restarts {self.state.max_restarts} reached after unresolved {det.detector}",
            )
            self._stop = True
            return
        cooldown = self.cfg.process.restart_policy.cooldown_seconds
        log().recovery(
            f"restart_policy fallback for {det.detector!r} — restart "
            f"{self.state.restarts + 1}/{self.state.max_restarts} "
            f"after {cooldown}s cooldown (no rule + no Claude action)"
        )
        if cooldown:
            time.sleep(cooldown)
        if self._stop:
            return
        try:
            status = self.supervisor.start()
        except Exception as e:  # noqa: BLE001
            log().error(f"restart_policy fallback failed to start child: {e}")
            return
        self.state.record_restart(
            reason=f"restart_policy fallback after unresolved {det.detector}",
            new_pid=status.pid,
        )
        self.state.pid = status.pid
        # Clear so the next exit registers as a fresh transition and re-fires
        # the detector path, rather than being treated as the same old exit.
        self.state.last_exit_code = None
        self._save_state()
        self._notify(
            "recovery",
            f"restart_policy fallback for {det.detector}",
            f"restart {self.state.restarts}/{self.state.max_restarts} — no healer action applied",
        )

    def _fire_detection(self, det: Detection) -> None:
        if det.kind == "anomaly":
            log().anomaly(f"[{det.detector}] {det.message}")
        else:
            log().error(f"[{det.detector}] {det.message}")
        # Signal the Slack dispatcher (if running) to poll the thread for
        # any human replies — this ties anomaly events to inbound polling so
        # the dispatcher doesn't have to spin its own constant loop.
        self._touch_inbound_marker()

        # If a pending verification matches this detector, the fix regressed.
        self._handle_potential_regression(det)

        # Refuse to attempt further fixes for a detector that has burned
        # through its budget. Still write the incident and notify.
        if det.detector in self._budget_paused or self._budget_exhausted(det.detector):
            log().recovery(
                f"healer budget exhausted for {det.detector!r} — recording incident "
                f"but not attempting another fix until a manual approve lands"
            )
            self._budget_paused.add(det.detector)
            outcome = None
        else:
            # When we're near the give-up threshold, force Claude before
            # the rule healer gets another shot. The rule path is fine
            # for fresh problems but if deterministic restarts have
            # already failed `escalate_to_claude_after` times we want the
            # heavier diagnosis on this attempt, not after we exhaust.
            self._maybe_flip_escalation()
            force_claude = self._escalation_active or det.detector in self._force_claude_next
            if force_claude:
                if det.detector in self._force_claude_next:
                    log().recovery(
                        f"forcing Claude diagnosis for {det.detector!r} — "
                        f"rules already regressed on this detector"
                    )
                    self._force_claude_next.discard(det.detector)
                else:
                    log().recovery(
                        f"forcing Claude diagnosis "
                        f"(unverified restarts {self.state.restarts}/{self.state.max_restarts}, "
                        f"threshold {self._escalation_threshold})"
                    )
                outcome = self.claude_healer.attempt(
                    det,
                    state_dict=self.state.model_dump(),
                    last_incident_dir=self.last_incident_dir,
                )
            else:
                outcome = self.rule_healer.attempt(det)
                if outcome is None:
                    log().recovery("no rule matched — escalating to Claude")
                    outcome = self.claude_healer.attempt(
                        det,
                        state_dict=self.state.model_dump(),
                        last_incident_dir=self.last_incident_dir,
                    )

        # Build and write the incident folder.
        frames = []
        if det.trace:
            frames = self.exploder.explode_trace(det.trace, lang_hint=det.meta.get("lang"))

        log_excerpt = list(self._log_buf)
        if det.log_context:
            # detector's local context tends to be most useful at the tail
            log_excerpt = log_excerpt + ["--- detector context ---", *det.log_context]

        config_paths = [self.cfg.resolve(p) for p in self.cfg.config_snapshots]
        incident_id: str | None = None
        write = IncidentWrite(
            kind=det.kind,
            detector=det.detector,
            message=det.message,
            process_kind=self.cfg.process.kind,
            command=self.cfg.process.command,
            pid=self.state.pid,
            restart_index=self.state.restarts,
            max_restarts=self.state.max_restarts,
            log_excerpt=log_excerpt,
            trace=det.trace,
            frames=frames,
            config_snapshot_paths=config_paths,
            state_snapshot=self.state.model_dump(),
            rule_match=(
                {"rule": outcome.rule_name, "source": outcome.source}
                if outcome and outcome.source == "rule"
                else None
            ),
            action=(outcome.action.model_dump() if outcome and outcome.action else None),
            claude_response=(outcome.claude_response if outcome else None),
            claude_diff=(outcome.claude_diff if outcome else None),
        )
        folder = self.incident_store.write(write)
        incident_id = folder.name
        self.last_incident_dir = folder
        if det.kind == "anomaly":
            self.state.record_anomaly(det.detector, det.message, incident_id=incident_id)

        # Notify.
        if (
            outcome
            and outcome.action
            and (outcome.action.notify if hasattr(outcome.action, "notify") else True)
        ):
            self._notify(
                "recovery" if det.kind == "error" else "anomaly",
                f"[{det.detector}] {det.message[:120]}",
                f"action={outcome.action.kind} source={outcome.source} incident={incident_id}",
                incident_id=incident_id,
            )
        else:
            self._notify(
                "anomaly" if det.kind == "anomaly" else "recovery",
                f"[{det.detector}] {det.message[:120]}",
                f"incident={incident_id} (no action applied)",
                incident_id=incident_id,
            )

        # Apply the action.
        if outcome is not None and outcome.action is not None:
            apply_started = time.monotonic()
            # For Claude-driven fixes we land the edits on a dedicated branch
            # so a regression can be reverted cleanly. For rule-driven
            # fixes (which only set env vars), branching adds no value.
            fix_branch: FixBranch | None = None
            if outcome.source == "claude" and self.cfg.healing.git.enabled:
                fix_branch = git_ops.create_fix_branch(
                    cwd=self.cfg.resolve(self.cfg.process.cwd),
                    cfg=self.cfg.healing.git,
                    incident_id=incident_id,
                )
                if fix_branch is not None and self.cfg.healing.git.write_enabled:
                    # Pin Claude's edits onto the branch.
                    git_ops.stage_and_commit_all(
                        cwd=self.cfg.resolve(self.cfg.process.cwd),
                        message=f"autosentry: claude fix for {incident_id}",
                    )

            # Record the attempt in the ledger as pending; verification
            # updates it later.
            source_label = outcome.rule_name or "claude"
            self.attempts.append(
                Attempt(
                    timestamp=now_iso(),
                    incident_id=incident_id,
                    detector=det.detector,
                    source=source_label,
                    branch=fix_branch.name if fix_branch else "",
                    status="pending",
                    duration_seconds=0.0,
                    description=det.message[:200],
                )
            )
            self._record_attempt_timestamp(det.detector)

            try:
                self.supervisor.apply_action(outcome.action)
                self.state.record_restart(
                    reason=det.message,
                    rule=outcome.rule_name,
                    new_pid=self.supervisor.status().pid,
                    incident_id=incident_id,
                )
                self.state.pid = self.supervisor.status().pid
                self._save_state()
            except Exception as e:  # noqa: BLE001
                log().error(f"apply_action failed: {e}")
                self.attempts.update(
                    incident_id,
                    source_label,
                    status="crashed",
                    duration_seconds=time.monotonic() - apply_started,
                )
                if fix_branch is not None:
                    git_ops.discard_branch(
                        cwd=self.cfg.resolve(self.cfg.process.cwd),
                        branch=fix_branch,
                        base_branch=self._base_branch(),
                    )
                return

            # Schedule verification: if the same detector re-fires within
            # ``verify_window_seconds`` we'll mark this attempt regressed.
            deadline = time.monotonic() + self.cfg.healing.verify_window_seconds
            self._pending_verify[det.detector] = (
                deadline,
                incident_id,
                source_label,
                fix_branch,
            )
        elif not self.supervisor.status().running:
            # No action applied (no rule matched, Claude healer timed out or
            # is disabled, or the budget is paused) AND the child is dead.
            # Fall back to the restart_policy budget so we don't wheel-spin
            # over a dead child waiting for someone to intervene by hand.
            # See issue #4. Anomaly detections on a live child don't qualify.
            self._restart_policy_fallback(det)

    def _notify(
        self,
        kind: str,
        title: str,
        body: str,
        *,
        incident_id: str | None = None,
    ) -> None:
        n = Notification(
            kind=kind,  # type: ignore[arg-type]
            title=title,
            body=body,
            incident_id=incident_id,
        )
        for notifier in self.notifiers:
            try:
                notifier.notify(n)
            except Exception as e:  # noqa: BLE001
                log().error(f"notifier {type(notifier).__name__} failed: {e}")

    def _save_state(self) -> None:
        try:
            self.state_store.save(self.state)
        except OSError as e:
            log().error(f"state save failed: {e}")

    # ----- verification + budget ------------------------------------------

    def _tick_verifications(self) -> None:
        """Resolve any pending fix attempts whose verify window has elapsed.

        Called from the monitor tick. Anything still ``pending`` past its
        deadline gets marked ``kept`` — meaning the fix worked, the detector
        didn't re-fire. Regressions are handled by the detection path.
        """
        if not self._pending_verify:
            return
        now = time.monotonic()
        expired: list[str] = []
        for detector_name, (deadline, _, _, _) in self._pending_verify.items():
            if now >= deadline:
                expired.append(detector_name)
        for detector_name in expired:
            deadline, incident_id, source, fix_branch = self._pending_verify.pop(detector_name)
            self.attempts.update(
                incident_id,
                source,
                status="kept",
                duration_seconds=self.cfg.healing.verify_window_seconds,
            )
            log().recovery(
                f"fix for {detector_name!r} verified — no recurrence in "
                f"{self.cfg.healing.verify_window_seconds}s"
            )
            if fix_branch is not None and self.cfg.healing.git.enabled:
                git_ops.keep_branch(
                    cwd=self.cfg.resolve(self.cfg.process.cwd),
                    branch=fix_branch,
                    base_branch=self._base_branch(),
                    auto_merge=self.cfg.healing.git.auto_merge,
                )
            # A successful heal means the supervisor is back in a healthy
            # state. Any subsequent detection is a fresh problem with a
            # fresh budget; reset the unverified-restart counter and clear
            # any active escalation.
            if self.state.restarts > 0:
                log().recovery(
                    f"verified fix — resetting unverified-restart counter "
                    f"(was {self.state.restarts}/{self.state.max_restarts}); "
                    f"all-time restarts={self.state.restarts_total}"
                )
            self.state.restarts = 0
            if self._escalation_active:
                self._escalation_active = False
                log().recovery("escalation cleared — rules back in play on next detection")
            self._save_state()

    def _handle_potential_regression(self, det: Detection) -> None:
        """If a detection matches a pending verification, mark regressed."""
        if det.detector not in self._pending_verify:
            return
        _, incident_id, source, fix_branch = self._pending_verify.pop(det.detector)
        action = self.cfg.healing.regression_action
        log().recovery(
            f"fix for {det.detector!r} REGRESSED (recurrence inside verify window) "
            f"— action={action}"
        )
        self.attempts.update(incident_id, source, status="regressed")
        # Rules just failed for this detector. Default posture is to
        # bring in the agent on the next attempt rather than recycle
        # rules. Claude-sourced regressions stay on the agent path
        # anyway — the flag is a no-op there.
        if source == "rules" and self.cfg.healing.escalate_on_rule_regression:
            self._force_claude_next.add(det.detector)
        if action == "revert" and fix_branch is not None and self.cfg.healing.git.enabled:
            git_ops.discard_branch(
                cwd=self.cfg.resolve(self.cfg.process.cwd),
                branch=fix_branch,
                base_branch=self._base_branch(),
            )
        if action == "escalate":
            self._budget_paused.add(det.detector)
            self._notify(
                "anomaly",
                f"[{det.detector}] regression — escalating",
                f"fix for incident {incident_id} regressed; pausing healer for this detector",
                incident_id=incident_id,
            )

    def _record_attempt_timestamp(self, detector: str) -> None:
        now = time.monotonic()
        bucket = self._recent_attempts.setdefault(detector, [])
        bucket.append(now)
        cutoff = now - 3600.0  # 1-hour rolling window
        self._recent_attempts[detector] = [t for t in bucket if t >= cutoff]

    def _budget_exhausted(self, detector: str) -> bool:
        limit = self.cfg.healing.budget.max_attempts_per_detector_per_hour
        if limit <= 0:
            return False
        bucket = self._recent_attempts.get(detector, [])
        return len(bucket) >= limit

    def _maybe_flip_escalation(self) -> None:
        """Activate the force-Claude path when we've burned enough
        unverified restarts. Notify once per episode so the operator
        sees it before the give-up email."""
        if self._escalation_active:
            return
        if self.state.restarts < self._escalation_threshold:
            return
        self._escalation_active = True
        self._notify(
            "recovery",
            "healer escalation",
            (
                f"unverified restarts {self.state.restarts}/{self.state.max_restarts} — "
                f"forcing Claude diagnosis on the next detection (threshold "
                f"{self._escalation_threshold}). Cleared on the next kept fix."
            ),
        )

    def _base_branch(self) -> str:
        """Return the branch we should treat as ``main`` for keep/revert.

        We capture this lazily because the user might be on a topic
        branch they want autosentry to land fixes against.
        """
        ctx = git_ops.context(self.cfg.resolve(self.cfg.process.cwd))
        return ctx.base_branch if ctx is not None else "main"

    # ----- dispatcher coordination ----------------------------------------

    def _consume_inbox(self) -> None:
        """Apply any human commands the dispatcher has appended to the inbox.

        Local file read; bails fast if the inbox doesn't exist. Errors are
        logged but never block the main loop.
        """
        try:
            apply_inbox_commands(self, self.inbox_path)
        except Exception as e:  # noqa: BLE001
            log().error(f"inbox consume failed: {e}")

    def _touch_inbound_marker(self) -> None:
        """Tell the dispatcher to poll Slack for replies on its next iteration.

        We use ``Path.touch()`` so the mtime advances; that's the signal the
        dispatcher watches for. Cheap (one ``utimes`` syscall) and safe — if
        no dispatcher is running, the file just sits there.
        """
        try:
            self.inbound_marker_path.parent.mkdir(parents=True, exist_ok=True)
            self.inbound_marker_path.touch(exist_ok=True)
        except OSError as e:
            log().error(f"inbound marker touch failed: {e}")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
