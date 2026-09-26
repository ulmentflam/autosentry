# Pipeline reliability and failure evidence

Implemented after the virtualenv change at `22b7286`.

## Behavior

- The pipeline controller runs each monitor in a separate spawned process.
  It owns the stage deadline and interruption handling. A stalled monitor cannot
  prevent timeout. Local process groups are cancelled; Docker and SLURM resource
  IDs are reported to the controller for bounded cancellation. Cancellation
  errors remain visible in the stage result. Attached processes remain externally
  owned. A per-project file lock prevents concurrent pipeline controllers.
- Resume creates a new run linked to its predecessor. Skipped stages must have
  completed and must match their recorded configuration fingerprints. Resume does
  not prove output integrity or source-code identity. Existing records without
  fingerprints require a fresh run.
- Launch checks use the child's cwd and effective PATH. Explicit requirements
  cover dependencies called by scripts. `run --check` checks every stage without
  launching work; `doctor` exposes the same checks. Runtime checks occur before
  each stage starts. Remote image and compute-node dependencies are not inspected.
- The rolling rate cap reserves launch attempts at the supervisor boundary.
  It covers automatic, rule, session, inbox, and clean-exit restart paths.
  State persists the window across monitor relaunches; verified fixes do not
  reset it. Stage transitions and explicit resets do.
- Each invocation retains its own run and stage journals, process logs, launch
  requirements, state snapshots, attempts ledger, and incident reports.
  `explain` exports bounded Markdown or JSON evidence for existing agents,
  prioritizing the failed stage and identifying omitted evidence. It never calls
  an LLM provider. Export redaction is best-effort, not a guarantee for arbitrary
  secrets printed by a workload. Original local logs retain the original output.

## Regression fixes discovered during validation

- Exit detection now distinguishes fast children that return the same exit code
  without an intervening running observation.
- Matching incidents in the same second receive distinct directories.
- Operator and recovery aborts return nonzero and prevent pipeline advancement.
- Local stdout archival drains independently of the detector queue and retains
  final output before a stage worker exits.
- Status exposes the stop reason; the TUI follows archived stage logs.

## Validation

Formatting, Ruff lint, Pyrefly, source/wheel builds, and Twine checks pass.
The full suite passes with 380 tests. Regression coverage includes real
subprocess stages, a blocked monitor that ignores SIGTERM, child cleanup,
resume/configuration rejection, launch environment resolution, restart-window
persistence, real rapid failure loops, concurrent-controller rejection, abort
exit codes, JSON export, redaction, bounded evidence, and saturated log queues.

The initial sandbox blocked fifteen HTTP tests from binding localhost sockets.
After permissions were restored, `make format` and `make ci` passed with all
380 tests, including those HTTP tests. Their assertions were not modified.

## Operator commands

```bash
autosentry run --check
autosentry run --resume
autosentry run --from-stage train
autosentry explain -o .autosentry/failure.md
autosentry explain --run-id <run-id> --json
```

Deadlines and rolling restart limits are opt-in. The README includes an unattended
pipeline example with both enabled and a stall detector. Hard controller death
(SIGKILL or host failure) still requires an external service manager or watchdog;
the next invocation may resume the recorded incomplete stage.
