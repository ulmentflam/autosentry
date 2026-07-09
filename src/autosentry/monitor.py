"""Monitor main loop.

Coordinates supervisor, detectors, healers, incident store, and
notifiers. The loop is intentionally simple and synchronous: one
thread of control, log lines pulled off the supervisor's queue,
detectors run on each line, detections fan out to healers, the
winning healer's action is applied via the supervisor, and the whole
event becomes an incident folder + a notification.
"""

from __future__ import annotations

import json
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
from autosentry.healers import ClaudeHealer, HealerOutcome, RuleHealer
from autosentry.inbox import apply_commands as apply_inbox_commands
from autosentry.incidents import IncidentStore, IncidentWrite, SourceExploder
from autosentry.ledger import Attempt, AttemptsLedger, now_iso
from autosentry.logger import SentryLogger, log
from autosentry.notifiers import build_notifiers
from autosentry.notifiers.base import Notification
from autosentry.state import MonitorState, StateStore, budget_exhausted, format_budget
from autosentry.supervisors import supervisor_for
from autosentry.supervisors.base import LogLine, ProcessStatus
from autosentry.vault import VaultStore
from autosentry.vault.narrator import Narrator
from autosentry.vault.patterns import PatternIndex

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
        # Vault writer + pattern detector. Wired in at the same lifecycle
        # points where the monitor writes incidents / records ledger
        # updates, so the markdown vault stays in sync with the
        # incidents/ + attempts.tsv on-disk state.
        self.vault: VaultStore | None = None
        self.patterns: PatternIndex | None = None
        self.narrator: Narrator | None = None
        if cfg.vault.enabled:
            vault_root = cfg.resolve(cfg.vault.path)
            self.vault = VaultStore(vault_root)
            self.patterns = PatternIndex(
                vault_root / ".patterns.json",
                threshold=cfg.vault.pattern_threshold,
                similarity=cfg.vault.similarity_threshold,
            )
            # Narrator is constructed unconditionally but only fires
            # when ``vault.narratives.enabled`` AND the provider key is
            # set (see ``Narrator.enabled``). Constructing it cheap;
            # it loads ``.narrated.json`` for dedup.
            self.narrator = Narrator(cfg.vault.narratives, vault_root)
        # Per-run vault state: the run id + the attempt counter per
        # incident so attempt notes get stable sub-paths.
        self._vault_run_id: str | None = None
        self._vault_child_index = 0
        self._vault_attempt_counters: dict[str, int] = {}
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
        # Pending verifications: detector → (deadline_monotonic,
        # incident_id, source, branch, attempt_index). attempt_index
        # lets vault resolution updates land on the right note.
        self._pending_verify: dict[str, tuple[float, str, str, FixBranch | None, int]] = {}
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
        # Exit code captured from the supervised child for the CLI to
        # propagate. ``None`` until the child exits.
        self._final_exit_code: int | None = None
        # State-save error-path bookkeeping. See ``_save_state`` —
        # without backoff + dedup, an iCloud evictor (or anything else
        # that races the atomic rename) can pin the monitor at ~11K
        # error log lines per second (issue #5).
        self._state_save_fail_count: int = 0
        self._state_save_next_attempt: float = 0.0
        self._state_save_last_error: str | None = None
        self._state_save_suppressed: int = 0
        # ``started_at`` stamp of the child the detectors are currently
        # tracking. Every supervisor sets a fresh stamp in ``start()``, so
        # when the supervisor swaps in a new child (rule/healer/session
        # restart, restart_policy fallback, or an external auto-restart)
        # this drifts from ``supervisor.status().started_at`` and we reset
        # per-child detector state — otherwise a stall detector carries the
        # dead child's frozen progress value forward and kill-loops the
        # healthy replacement (issue #9). Keyed on ``started_at`` rather
        # than pid because some supervisors (docker) report ``pid=None``.
        # ``None`` until the first child is spawned.
        self._detector_child_started_at: str | None = None

    def _resolve_escalation_threshold(self) -> int:
        explicit = self.cfg.healing.escalate_to_claude_after
        if explicit is not None and explicit > 0:
            return explicit
        # Default: 2 unverified restarts before Claude takes over.
        # Decoupled from ``max_restarts`` so the unlimited-budget
        # default doesn't push Claude escalation off to infinity —
        # rules get two cheap shots at known transients, then the
        # agentic flow runs as the main fix path. Override via
        # ``healing.escalate_to_claude_after``.
        return 2

    # ----- public lifecycle -------------------------------------------------

    def run(self) -> int:
        """Run the monitor loop. Returns the exit code the CLI should
        propagate — the supervised child's last exit code when known,
        otherwise 0. See issue #5: in ``one_shot`` /
        ``restart_on_failure`` lifecycles the supervisor exits with the
        child instead of sitting idle forever.
        """
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
        # The detectors are, from now on, tracking this child. Seed the
        # restart-watch stamp so the first genuine restart (not this initial
        # spawn) triggers a detector reset. See ``_reset_detectors_for_restart``.
        self._detector_child_started_at = status.started_at
        self._save_state()
        # Vault: write the supervisor-session note + the initial child-
        # run note for this first process spawn. Subsequent restarts
        # record their own child-run nodes from the action-apply path.
        if self.vault is not None and self.state.started_at:
            self._vault_run_id = self._vault_run_id_for(self.state.started_at)
            self.vault.record_run_start(
                run_id=self._vault_run_id,
                supervisor_kind=self.cfg.process.kind,
                command=self.cfg.process.command,
                started_at=self.state.started_at,
                cwd=str(self.cfg.resolve(self.cfg.process.cwd)),
            )
            self._record_vault_child_restart(
                pid=status.pid,
                started_at=status.started_at or self.state.started_at,
                reason="initial start",
            )

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
        return self._final_exit_code or 0

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

    def _reset_detectors_for_restart(self, status: ProcessStatus) -> None:
        """Reset per-child detector state when the child has been swapped.

        Called once per tick. Covers every restart path — rule/healer/
        session actions, the restart_policy fallback, and external
        auto-restarts — with one check rather than threading a hook through
        each call site. Keyed on ``started_at`` (fresh per ``start()`` in
        every supervisor); a no-op while it is unchanged or the child is
        momentarily absent, so a brief gap between stop and start doesn't
        spuriously reset twice.
        """
        new_stamp = status.started_at
        if not status.running or new_stamp is None or new_stamp == self._detector_child_started_at:
            return
        log().info(
            f"child restart detected (started_at {self._detector_child_started_at} → "
            f"{new_stamp}) — resetting detector state"
        )
        for det in self.detectors:
            det.on_child_restart()
        self._detector_child_started_at = new_stamp

    def _tick(self) -> None:
        status = self.supervisor.status()
        # Before running the detectors, notice whether the child was
        # swapped out since the last tick and give detectors a clean slate
        # if so (issue #9).
        self._reset_detectors_for_restart(status)
        for det in self.detectors:
            d = det.observe_status(status)
            if d is not None:
                self._fire_detection(d)
            d = det.observe_tick()
            if d is not None:
                self._fire_detection(d)
        # In session-dispatch mode the interactive session decides
        # which action to apply for each incident and asks the monitor
        # to actually run it via the action queue. Cheap file read;
        # only ticks in this mode.
        if self.cfg.dispatch.mode == "session":
            self._drain_session_action_queue()

    def _drain_session_action_queue(self) -> None:
        """Apply any session-queued actions the monitor hasn't seen yet.

        Each line in ``session_actions.jsonl`` is a JSON object with::

            {
                "id": "<unique-id, monotonic per session>",
                "incident_id": "<the incident this resolves>",
                "rule": "<rule name or null>",
                "source": "rule" | "claude" | "manual",
                "action": {"kind": "...", "set": {...}, "command": [...]},
            }

        The monitor applies each new line via ``supervisor.apply_action``
        and advances the cursor. Failures are logged but never block the
        main loop. We deliberately don't write a result file in this
        first cut — the session reads ``state.json`` / the structured
        log to confirm; result-tracking can come in a follow-up.
        """
        from autosentry.config import RuleAction

        queue_path = self.cfg.resolve(self.cfg.dispatch.action_queue_path)
        if not queue_path.exists():
            return
        cursor_path = self.cfg.resolve(self.cfg.dispatch.action_cursor_path)
        last_id = ""
        if cursor_path.exists():
            try:
                last_id = cursor_path.read_text(encoding="utf-8").strip()
            except OSError:
                last_id = ""
        try:
            lines = queue_path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            log().error(f"session action queue read failed: {e}")
            return
        applied_id: str | None = None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                log().error(f"session action queue: skipping malformed line: {line[:120]}")
                continue
            entry_id = str(entry.get("id", ""))
            if not entry_id or entry_id <= last_id:
                continue
            try:
                action = RuleAction.model_validate(entry.get("action") or {})
            except Exception as e:  # noqa: BLE001
                log().error(f"session action {entry_id}: invalid action — {e}")
                applied_id = entry_id
                continue
            log().recovery(
                f"session-dispatch applying {action.kind} "
                f"(incident={entry.get('incident_id')}, source={entry.get('source', 'manual')})"
            )
            try:
                self.supervisor.apply_action(action)
                self.state.record_restart(
                    reason=f"session-dispatch action {action.kind}",
                    rule=entry.get("rule"),
                    new_pid=self.supervisor.status().pid,
                    incident_id=entry.get("incident_id"),
                )
                self.state.pid = self.supervisor.status().pid
                self._save_state()
            except Exception as e:  # noqa: BLE001
                log().error(f"session-dispatch apply_action failed: {e}")
            applied_id = entry_id
        if applied_id and applied_id != last_id:
            try:
                cursor_path.parent.mkdir(parents=True, exist_ok=True)
                cursor_path.write_text(applied_id, encoding="utf-8")
            except OSError as e:
                log().error(f"session action cursor write failed: {e}")

    def _handle_exit(self, exit_code: int) -> bool:
        """Return True to continue (after restart), False to stop the monitor."""
        log().info(f"process exited with code {exit_code}")
        self._final_exit_code = exit_code
        # Lifecycle gate (issue #5). A clean exit used to leave the monitor
        # ticking forever; ``one_shot`` and the default ``restart_on_failure``
        # now stop the supervisor instead of sitting idle.
        lifecycle = self.cfg.process.lifecycle
        if lifecycle == "one_shot":
            log().info(
                f"lifecycle=one_shot — supervised work complete (exit {exit_code}); "
                f"shutting supervisor down"
            )
            self._notify(
                "exit",
                "supervised work complete",
                f"lifecycle=one_shot — exit code {exit_code}",
            )
            return False
        if lifecycle == "restart_on_failure" and exit_code == 0:
            log().info(
                "lifecycle=restart_on_failure and child exited cleanly — "
                "shutting supervisor down (set lifecycle: restart_always to keep restarting)"
            )
            self._notify(
                "exit",
                "supervised work complete",
                "lifecycle=restart_on_failure — child exited cleanly (code 0)",
            )
            return False
        # exit_code detector picks this up too, but we may need to call it explicitly
        # to give a detection if the user didn't configure one. Let detectors fire on
        # status; the actual restart decision goes through the standard healer path.
        # If no healer fires and the unverified-restart budget hasn't
        # been capped (the default), keep watching. Only stop when an
        # explicit cap has been set and we've burned through it.
        if budget_exhausted(self.state.restarts, self.state.max_restarts):
            log().error(f"max restarts ({self.state.max_restarts}) reached — giving up")
            self._notify(
                "exit",
                "max restarts reached",
                f"giving up after {self.state.restarts} restart(s)",
            )
            self._record_vault_exhaustion(detector=None)
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
        if budget_exhausted(self.state.restarts, self.state.max_restarts):
            log().error(
                f"no recovery applied for {det.detector!r} and max restarts "
                f"({self.state.max_restarts}) reached — stopping monitor"
            )
            self._notify(
                "exit",
                "recovery exhausted",
                f"max restarts {self.state.max_restarts} reached after unresolved {det.detector}",
            )
            self._record_vault_exhaustion(detector=det.detector)
            self._stop = True
            return
        cooldown = self.cfg.process.restart_policy.cooldown_seconds
        log().recovery(
            f"restart_policy fallback for {det.detector!r} — restart "
            f"{self.state.restarts + 1}/{format_budget(self.state.max_restarts)} "
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
            (
                f"restart {self.state.restarts}/{format_budget(self.state.max_restarts)} "
                "— no healer action applied"
            ),
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

        # In session-dispatch mode the in-process healer doesn't run —
        # the interactive Claude Code session reads pending incidents
        # via ``autosentry watch --once`` and dispatches healers itself.
        # We still write the incident folder, update state, and let the
        # restart_policy safety net keep a dead child alive if the
        # session isn't around to react.
        session_dispatch = self.cfg.dispatch.mode == "session"

        # Refuse to attempt further fixes for a detector that has burned
        # through its budget. Still write the incident and notify.
        if session_dispatch:
            outcome = None
        elif det.detector in self._budget_paused or self._budget_exhausted(det.detector):
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
                    budget = format_budget(self.state.max_restarts)
                    log().recovery(
                        f"forcing Claude diagnosis "
                        f"(unverified restarts {self.state.restarts}/{budget}, "
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
        # Vault: classify against the pattern index, then write the
        # incident note + (if the pattern threshold just got crossed)
        # the pattern aggregator. Best-effort — failures log and the
        # supervisor keeps running.
        self._record_vault_incident(det, incident_id, folder, fired_at=_now_iso())
        # Under session-dispatch mode, signal the interactive session
        # that there's a new pending incident to handle. The mtime of
        # this marker is the wake signal; the incident folder itself
        # carries the payload.
        if session_dispatch:
            self._touch_session_dispatch_marker()

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

            # Vault: write the attempt note. Increments the per-
            # incident attempt counter so attempt indices are stable
            # across multiple healer attempts on the same incident.
            attempt_index = self._next_attempt_index(incident_id)
            self._record_vault_attempt_start(
                incident_id=incident_id,
                attempt_index=attempt_index,
                outcome=outcome,
                fix_branch=fix_branch,
            )

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
                # The action just kicked off a new child process — log
                # that as a child-restart node in the vault.
                self._record_vault_child_restart(
                    pid=self.supervisor.status().pid,
                    started_at=_now_iso(),
                    reason=f"healer fix for {incident_id}",
                )
            except Exception as e:  # noqa: BLE001
                log().error(f"apply_action failed: {e}")
                self.attempts.update(
                    incident_id,
                    source_label,
                    status="crashed",
                    duration_seconds=time.monotonic() - apply_started,
                )
                self._record_vault_attempt_resolution(
                    incident_id=incident_id,
                    attempt_index=attempt_index,
                    status="crashed",
                    notes=f"apply_action raised: {e}",
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
                attempt_index,
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

    # Capped exponential backoff for state-save retries. With a 0.5s base
    # and 60s cap the failure log goes: now, +0.5s, +1s, +2s, … +60s, +60s,
    # not 11K lines/second (issue #5).
    _STATE_SAVE_BACKOFF_BASE = 0.5
    _STATE_SAVE_BACKOFF_CAP = 60.0

    def _save_state(self) -> None:
        now = time.monotonic()
        if self._state_save_fail_count > 0 and now < self._state_save_next_attempt:
            # Inside a backoff window — drop the write and count it so the
            # next emitted error can say how many we suppressed.
            self._state_save_suppressed += 1
            return
        try:
            self.state_store.save(self.state)
        except OSError as e:
            self._handle_state_save_failure(e)
            return
        if self._state_save_fail_count > 0:
            log().info(
                f"state save recovered after {self._state_save_fail_count} "
                f"failure(s) (suppressed {self._state_save_suppressed} retry log lines)"
            )
        self._state_save_fail_count = 0
        self._state_save_next_attempt = 0.0
        self._state_save_last_error = None
        self._state_save_suppressed = 0

    def _handle_state_save_failure(self, exc: OSError) -> None:
        """Apply exponential backoff and dedup repeat errors.

        Without this the ``state save failed`` line can fire on every loop
        tick. In one observed run that produced ~16M log lines in 24
        minutes when an iCloud evictor kept removing ``state.json.tmp``
        between write and rename (issue #5).
        """
        msg = f"{type(exc).__name__}: {exc}"
        same_as_last = msg == self._state_save_last_error
        self._state_save_fail_count += 1
        self._state_save_last_error = msg
        delay = min(
            self._STATE_SAVE_BACKOFF_CAP,
            self._STATE_SAVE_BACKOFF_BASE * (2 ** (self._state_save_fail_count - 1)),
        )
        self._state_save_next_attempt = time.monotonic() + delay
        if not same_as_last:
            # First time we've seen this error in the current burst — log it
            # so operators see what's failing.
            log().error(
                f"state save failed: {exc} — retrying with backoff (next attempt in {delay:.1f}s)"
            )
            self._state_save_suppressed = 0
        else:
            # Same error as before — only emit when the backoff has actually
            # climbed to the cap, and roll up any suppressed retries.
            suppressed = self._state_save_suppressed
            self._state_save_suppressed = 0
            if delay >= self._STATE_SAVE_BACKOFF_CAP:
                log().error(
                    f"state save still failing ({self._state_save_fail_count} attempts, "
                    f"{suppressed} log lines suppressed since last report): {exc}"
                )

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
        for detector_name, (deadline, _, _, _, _) in self._pending_verify.items():
            if now >= deadline:
                expired.append(detector_name)
        for detector_name in expired:
            (
                deadline,
                incident_id,
                source,
                fix_branch,
                attempt_index,
            ) = self._pending_verify.pop(detector_name)
            self.attempts.update(
                incident_id,
                source,
                status="kept",
                duration_seconds=self.cfg.healing.verify_window_seconds,
            )
            self._record_vault_attempt_resolution(
                incident_id=incident_id,
                attempt_index=attempt_index,
                status="kept",
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
                    f"(was {self.state.restarts}/{format_budget(self.state.max_restarts)}); "
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
        (
            _,
            incident_id,
            source,
            fix_branch,
            attempt_index,
        ) = self._pending_verify.pop(det.detector)
        action = self.cfg.healing.regression_action
        log().recovery(
            f"fix for {det.detector!r} REGRESSED (recurrence inside verify window) "
            f"— action={action}"
        )
        self.attempts.update(incident_id, source, status="regressed")
        self._record_vault_attempt_resolution(
            incident_id=incident_id,
            attempt_index=attempt_index,
            status="regressed",
        )
        self._record_vault_regression(
            incident_id=incident_id,
            detector=det.detector,
            original_attempt=attempt_index,
        )
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
        budget = format_budget(self.state.max_restarts)
        self._notify(
            "recovery",
            "healer escalation",
            (
                f"unverified restarts {self.state.restarts}/{budget} — "
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

    # ----- vault helpers --------------------------------------------------

    @staticmethod
    def _vault_run_id_for(started_at: str) -> str:
        """Stable id for the supervisor session, derived from its
        start timestamp. Used as the vault note filename."""
        # Replace colons + dots so the id is a clean filename slug.
        return started_at.replace(":", "-").replace(".", "-")

    def _current_child_run_id(self) -> str | None:
        if self._vault_run_id is None or self._vault_child_index == 0:
            return None
        return f"{self._vault_run_id}-child-{self._vault_child_index}"

    def _record_vault_child_restart(
        self,
        *,
        pid: int | None,
        started_at: str,
        reason: str | None,
    ) -> None:
        """Bump the child-restart counter and write the child-run note.
        Safe to call when ``self.vault`` is None."""
        if self.vault is None or self._vault_run_id is None:
            return
        self._vault_child_index += 1
        self.vault.record_child_restart(
            run_id=self._vault_run_id,
            child_index=self._vault_child_index,
            pid=pid,
            started_at=started_at,
            reason=reason,
        )

    def _record_vault_incident(
        self,
        det: Detection,
        incident_id: str,
        folder: Path,
        *,
        fired_at: str,
    ) -> None:
        """Classify the incident against the pattern index, write the
        incident summary note, update the detector aggregator, and
        (if a pattern threshold was just crossed) refresh the pattern
        note. All best-effort; failures log and the supervisor keeps
        running."""
        if self.vault is None or self.patterns is None:
            return
        try:
            classification = self.patterns.classify(
                incident_id=incident_id,
                detector=det.detector,
                message=det.message,
                trace=det.trace or None,
            )
            incident_folder_rel = str(folder.relative_to(self.vault.root.parent.parent))
        except Exception as e:  # noqa: BLE001
            log().error(f"vault: classify failed for incident {incident_id}: {e}")
            return

        pattern_slug = classification.pattern.slug if classification.pattern is not None else None
        self.vault.record_incident(
            incident_id=incident_id,
            run_id=self._vault_run_id or "unknown-run",
            child_run_id=self._current_child_run_id(),
            detector=det.detector,
            kind=det.kind,
            message=det.message,
            fired_at=fired_at,
            trace_hash=classification.trace_hash,
            pattern_slug=pattern_slug,
            incident_folder_rel=incident_folder_rel,
        )
        # Refresh pattern note whenever the index touched it — either a
        # threshold-crossing promotion or an additional incident joining
        # an existing pattern. Idempotent rewrite.
        if classification.pattern is not None:
            self.vault.record_pattern(
                slug=classification.pattern.slug,
                detector=classification.pattern.detector,
                representative_message=classification.pattern.representative_message,
                incident_ids=classification.pattern.incident_ids,
                trace_hash=classification.pattern.trace_hash,
            )
            # First time this pattern crossed the threshold? Replace
            # the templated narrative with LLM prose (if the narrator
            # is enabled + the provider key is set). Best-effort.
            if classification.is_new_pattern and self.narrator is not None:
                self._maybe_narrate_pattern(classification.pattern)

    def _next_attempt_index(self, incident_id: str) -> int:
        n = self._vault_attempt_counters.get(incident_id, 0) + 1
        self._vault_attempt_counters[incident_id] = n
        return n

    def _record_vault_attempt_start(
        self,
        *,
        incident_id: str,
        attempt_index: int,
        outcome: HealerOutcome,
        fix_branch: FixBranch | None,
    ) -> None:
        if self.vault is None:
            return
        self.vault.record_attempt_start(
            incident_id=incident_id,
            attempt_index=attempt_index,
            source=outcome.source,
            rule_name=outcome.rule_name,
            action_kind=outcome.action.kind if outcome.action else "(no-action)",
            action_set=dict(outcome.action.set) if outcome.action else {},
            started_at=now_iso(),
            branch=fix_branch.name if fix_branch else None,
        )

    def _record_vault_attempt_resolution(
        self,
        *,
        incident_id: str,
        attempt_index: int,
        status: str,
        notes: str | None = None,
    ) -> None:
        if self.vault is None:
            return
        self.vault.record_attempt_resolution(
            incident_id=incident_id,
            attempt_index=attempt_index,
            status=status,
            ended_at=now_iso(),
            notes=notes,
        )

    def _record_vault_regression(
        self,
        *,
        incident_id: str,
        detector: str,
        original_attempt: int,
    ) -> None:
        if self.vault is None:
            return
        ref = self.vault.record_regression(
            incident_id=incident_id,
            detector=detector,
            original_attempt=original_attempt,
            re_fire_at=now_iso(),
        )
        # First regression for this incident gets an LLM narrative.
        if ref is not None and self.narrator is not None:
            self._maybe_narrate_regression(
                note_path=ref.path,
                incident_id=incident_id,
                detector=detector,
                original_attempt=original_attempt,
            )

    def _record_vault_exhaustion(self, *, detector: str | None) -> None:
        if self.vault is None or self._vault_run_id is None:
            return
        ref = self.vault.record_exhaustion(
            run_id=self._vault_run_id,
            final_restart_count=self.state.restarts,
            max_restarts=self.state.max_restarts,
            final_detector=detector,
            ended_at=now_iso(),
        )
        if ref is not None and self.narrator is not None:
            self._maybe_narrate_exhaustion(
                note_path=ref.path,
                run_id=self._vault_run_id,
                final_restart_count=self.state.restarts,
                max_restarts=self.state.max_restarts,
                final_detector=detector,
            )

    # ----- narrator integration -------------------------------------------

    def _maybe_narrate_pattern(self, pattern) -> None:  # noqa: ANN001
        """First-occurrence narrator for a new pattern. Returns
        silently if the narrator is disabled or the LLM call fails;
        replaces the templated narrative on success."""
        if self.narrator is None or self.vault is None:
            return
        narrative = self.narrator.narrate_pattern(
            slug=pattern.slug,
            detector=pattern.detector,
            representative_message=pattern.representative_message,
            incident_count=len(pattern.incident_ids),
        )
        if narrative is None:
            return
        self.vault.replace_narrative(
            self.vault.root / "patterns" / f"pattern-{pattern.slug}.md",
            narrative,
        )

    def _maybe_narrate_regression(
        self,
        *,
        note_path: Path,
        incident_id: str,
        detector: str,
        original_attempt: int,
    ) -> None:
        if self.narrator is None or self.vault is None:
            return
        narrative = self.narrator.narrate_regression(
            incident_id=incident_id,
            detector=detector,
            original_attempt=original_attempt,
        )
        if narrative is None:
            return
        self.vault.replace_narrative(note_path, narrative)

    def _maybe_narrate_exhaustion(
        self,
        *,
        note_path: Path,
        run_id: str,
        final_restart_count: int,
        max_restarts: int,
        final_detector: str | None,
    ) -> None:
        if self.narrator is None or self.vault is None:
            return
        narrative = self.narrator.narrate_exhaustion(
            run_id=run_id,
            final_restart_count=final_restart_count,
            max_restarts=max_restarts,
            final_detector=final_detector,
        )
        if narrative is None:
            return
        self.vault.replace_narrative(note_path, narrative)

    def _touch_session_dispatch_marker(self) -> None:
        """Signal the interactive Claude Code session that a new incident
        is pending for it to handle. Under ``dispatch.mode: session``,
        the session's Stop hook (or ``autosentry watch --once``) reads
        this marker's mtime to know when to wake up. Cheap; cousin of
        :meth:`_touch_inbound_marker`.
        """
        marker = self.cfg.resolve(self.cfg.dispatch.request_marker)
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch(exist_ok=True)
        except OSError as e:
            log().error(f"session-dispatch marker touch failed: {e}")

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
