# Changelog

All notable changes to autosentry are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.13.2] — 2026-07-08

### Fixed

- **Stall detector no longer kill-loops a healthy process after a
  restart ([#9]).** Two bugs compounded here. First, the local
  supervisor's log iterator terminated on the *first* child's
  end-of-stream sentinel, so after any restart the monitor stopped
  consuming log lines entirely — detectors never saw the new child's
  output. The iterator now treats the sentinel like a quiet tick and
  keeps reading the shared queue (real exits are still detected via
  `status()`). Second, detectors carried per-child state across
  restarts: a `stall` detector kept the dead child's last metric value
  and no-progress clock, then fired a spurious stall
  `no_progress_seconds` after every restart, restarting a perfectly
  healthy child until `max_restarts` was exhausted. Detectors now get
  an `on_child_restart()` lifecycle hook; the monitor calls it whenever
  the supervised child is swapped (rule/healer/session action,
  `restart_policy` fallback, or external auto-restart), and the stall
  detector uses it to reset its tracked value and clocks so the fresh
  child is observed from a clean slate.

[#9]: https://github.com/ulmentflam/autosentry/issues/9

## [0.13.1] — 2026-06-04

### Changed

- **Higher default budgets — autosentry now leans toward "keep
  trying" out of the box.** The previous defaults gave up too
  quickly for long-running workloads (ML training, multi-hour data
  pipelines) while doing nothing to prevent API spam from a
  genuinely stuck healer. New defaults:

  | knob                                            | was   | now    | meaning |
  |-------------------------------------------------|-------|--------|---------|
  | `process.restart_policy.max_restarts`           | `10`  | `50`   | consecutive unverified restarts before giving up |
  | `healing.budget.max_attempts_per_detector_per_hour` | `5` | `60` | rate-cap on healer attempts per detector (1/min) |
  | `healing.budget.max_wall_seconds_per_incident`  | `600` | `7200` | 2-hour budget per incident |

  Existing configs keep their explicit values — only fresh inits and
  `init --upgrade` pick up the new defaults (and `--upgrade` prompts
  per-key, so you can decline).

- **`max_restarts` is now documented as a *kill-switch*, not a
  budget.** The counter (`state.restarts`) zeros on every fix that
  resolves as `kept` in the attempts ledger, so a productive healer
  runs the supervisor indefinitely; `max_restarts` only trips when
  the healer can't land a kept fix for that many restarts in a row.
  Set it to `0` to disable the kill-switch entirely (the supervisor
  will keep restarting until something external stops it).

- **`healing.escalate_to_claude_after` decouples from
  `max_restarts // 5`.** Previously the escalation threshold scaled
  with the cap (so a higher `max_restarts` would push Claude
  escalation later — exactly wrong). Now it falls back to a literal
  `2` regardless of `max_restarts`: rules get two cheap shots at
  known transients, then the agentic flow takes over.

- New helpers in `autosentry.state`: `budget_exhausted(restarts,
  max_restarts)` (single source of truth for the kill-switch check;
  honors the `0 = unlimited` sentinel) and `format_budget(max_restarts)`
  (renders `∞` for the unlimited case, otherwise the integer).
  Threaded through Monitor, status, TUI, doctor, and the incident
  report so every surface displays the same thing.

### Internal

- `MonitorState.max_restarts` default `10 → 50` to match
  `RestartPolicy.max_restarts`. Existing `state.json` files are
  unaffected — the value is overridden from cfg on every Monitor
  start.

## [0.13.0] — 2026-06-04

### Added

- **`autosentry reset` subcommand** — clear the unverified-restart
  counter without losing audit history. When the monitor has parked
  itself with "max restarts reached" and you've fixed the underlying
  problem out-of-band, run `autosentry reset` to:

  1. SIGTERM the supervised child if its pid is still alive (skipped
     when nothing is running).
  2. Zero `state.restarts` while preserving `restarts_total` and
     `restart_history` so the prior failures stay readable.
  3. Append a `ResetRecord` to `state.json` (`reset_history`) and
     mirror a one-line entry to `.autosentry/logs/reset.log` for
     `tail -F`-friendly auditing.
  4. Relaunch the supervisor in the foreground (or `--no-restart` to
     clear-and-exit).

  Flags: `--reason "<text>"` lands in both the structured record and
  the plain-text log. `--full --force` also drops `restart_history`
  for a complete wipe. `autosentry status` now surfaces a `recent
  resets` table next to `recent restarts`.

- **`process.stages` — multi-step pipelines.** Configure two or more
  commands that run sequentially under one supervisor (e.g. an ML
  pipeline: pretrain → SFT → GRPO):

  ```yaml
  process:
    kind: local
    stages:
      - name: pretrain
        command: [python, train.py, --phase, pretrain]
        restart_policy: { max_restarts: 3 }
      - name: sft
        command: [python, train.py, --phase, sft]
      - name: grpo
        command: [python, train.py, --phase, grpo]
  ```

  Each stage waits for the previous to exit 0; restart budgets reset
  between stages (logged as `source="pipeline"` ResetRecords so the
  audit trail is intact). A mid-pipeline failure aborts the rest;
  remaining stages are marked `skipped` in `.autosentry/pipeline.json`.
  Detectors and rules are shared across stages — the common case
  (same script, different phases) stays ergonomic. `process.stages`
  and `process.command` are mutually exclusive (config validator
  rejects both being set). `autosentry status` shows a stage-by-stage
  progress table when stages are configured.

- **Interactive `autosentry init` over an existing config now prompts**
  instead of hard-exiting. When run inside a TTY (Claude Code, an
  operator terminal) on top of an existing `.autosentry/autosentry.yaml`,
  the command offers reset / upgrade / cancel rather than the previous
  `exit 1`. Non-interactive invocations and CI still error out cleanly
  with the original message.

- **`pi` (pi.dev) added to `autosentry skills install`** — drops the
  `/autosentry`, `/autosentry-init`, and `/autosentry-update` prompt
  templates into `.pi/prompts/` (or `~/.pi/agent/prompts/` with
  `--scope global`). Pi joins Claude Code, OpenCode, Codex, Gemini,
  Cursor, and Zed as a first-class supported editor.

### Changed

- `MonitorState` gains `reset_history: list[ResetRecord]` (capped at
  200 like `restart_history`). State files written by 0.12.x load
  unchanged; the field defaults to `[]`.
- `ClaudeHealer._SKILL_MARKERS` recognizes `.pi/prompts/autosentry.md`
  so interactive recovery mode activates for Pi sessions.

## [0.12.0] — 2026-05-31

### Added

- **`autosentry doctor --fix` auto-repair** plus 11 new diagnostic
  checks covering layout & migration, corrupt state files, integration
  health, and steering. Designed for old or partially-broken installs:

  | check                       | what it catches                            | repair |
  |-----------------------------|--------------------------------------------|--------|
  | legacy config               | pre-0.8 root-level `autosentry.yaml`       | move to `.autosentry/` |
  | `.autosentry` tree          | missing `incidents/` / `logs/` / `prompts/` | recreate dirs |
  | vault dir                   | missing `.autosentry/vault/`               | rebuild from `incidents/index.jsonl` |
  | `state.json`                | unparseable JSON                           | rotate aside as `.broken-<ts>` |
  | `attempts.tsv`              | malformed rows                             | rotate aside |
  | `incidents/index.jsonl`     | malformed JSONL lines                      | rotate aside |
  | stop hook                   | `dispatch.mode: session` without hook      | install via `autosentry hooks install` |
  | langgraph api keys          | provider key missing                       | (no auto-fix; surfaced loud) |
  | recovery request            | orphaned `recovery_request.md` blocking runs | rotate to `.stale-<ts>` |
  | `claude.mode` steering      | deprecated `subprocess` mode               | (no fix; explains alternatives) |

  `--fix` is idempotent — running it twice is a no-op the second time.
  Sensitive things (API keys, config edits) are never auto-changed.

- **LLM-generated vault narratives** for first-occurrence significant
  events. New `vault.narratives: {enabled, provider, model}` config
  (off by default) reuses the LangGraph provider factory from 0.10.0.
  When a pattern crosses its threshold for the first time, when an
  incident first regresses, or when a run first exhausts its restart
  budget, the narrator fires a single LLM call and replaces the
  templated `## Narrative` section in the relevant note. Dedup state
  lives in `.autosentry/vault/.narrated.json` — subsequent same-class
  events use the templated narrative without additional LLM calls.
  Best-effort: a failed narration logs and returns; the note keeps
  its templated paragraph.

- **Vault rendering in `autosentry web`** plus a Mermaid graph view.
  New routes:
  - `GET /vault` — categorized index of every vault note.
  - `GET /vault/<subdir>/<file>` — renders the markdown note with
    Obsidian `[[wikilinks]]` resolved to in-app URLs. Nested notes
    (attempts, child runs) are addressed via the wikilink ID
    convention (`<parent>-<child>`).
  - `GET /vault/graph` — Mermaid `graph TD` of supervisor sessions →
    child restarts on one axis, and incidents → attempts → outcomes
    on the other, with pattern aggregator dotted edges. Click any
    node to drill into its vault note.

### Changed

- `record_pattern` in `VaultStore` is now an incremental update for
  existing pattern notes (appends new incidents, bumps the count)
  rather than a full re-render. Preserves any LLM narrative the
  narrator has injected — a full re-render was clobbering it back to
  the templated prose.

## [0.11.0] — 2026-05-31

### Added

- **Obsidian-compatible markdown vault.** Inspired by claude-obsidian:
  autosentry now writes human-readable, wikilinked markdown summaries
  of significant supervisor events to `.autosentry/vault/`. Open the
  directory in Obsidian for the graph view; everything is plain
  markdown that needs no plugin to render.

  File layout:

  ```
  .autosentry/vault/
  ├── index.md
  ├── runs/<run-id>.md
  ├── runs/<run-id>/child-<n>.md          ← every supervised child restart
  ├── incidents/<id>.md
  ├── incidents/<id>/attempt-<n>.md       ← every healer attempt
  ├── detectors/<name>.md                  ← per-detector aggregator
  ├── patterns/<slug>.md                   ← recurring failure modes
  ├── regressions/<incident-id>.md         ← fixes that didn't stick
  └── exhaustions/<run-id>.md              ← runs that gave up
  ```

  Two sub-tree dimensions: **supervisor sessions branch into child
  process restarts**, and **incidents branch into attempt chains**.
  The graph view in Obsidian renders the full DAG out of the box.

- **Significant-event detection.**
  - **Patterns**: trace-hash matching (SHA-256 of a normalized trace —
    strips line numbers, hex addresses, PIDs, timestamps, path prefixes)
    plus message-similarity grouping (same detector + Levenshtein
    distance within `vault.similarity_threshold * max(len(a), len(b))`).
    Once `vault.pattern_threshold` (default 3) incidents match, autosentry
    creates a `patterns/<slug>.md` aggregator note linking all of them.
    Subsequent same-trace incidents join the existing pattern immediately.
  - **Regressions**: when a fix re-fires inside the verify window,
    `regressions/<incident-id>.md` documents the rollback.
  - **Exhaustions**: when `max_restarts` is hit, `exhaustions/<run-id>.md`
    summarizes why autosentry stopped trying.
  - **Same-bug crashes**: trace-hash matching catches identical bugs
    across different incidents even when messages differ slightly.

- **Vault config.** New `vault: { enabled, path, pattern_threshold,
  similarity_threshold }` block. Vault is enabled by default; set
  `vault.enabled: false` to opt out entirely. All vault writes are
  best-effort — a vault failure logs an error but never blocks the
  monitor loop.

### Notes

- Templated narratives only in 0.11.0. LLM-generated narratives for
  "first occurrence of a pattern / regression / exhaustion" are queued
  for 0.11.1 (they reuse the LangGraph provider factory from 0.10.0;
  free under `dispatch.mode: session`).
- Web viewer (`autosentry web`) doesn't render the vault yet — that's
  the 0.12.0 PR. For now, open the directory in Obsidian directly.

## [0.10.0] — 2026-05-31

### Added

- **LangGraph-powered headless healer.** New `healing.claude.mode:
  langgraph` value (with a sibling `healing.langgraph` config block)
  replaces the single-shot `claude --print` subprocess healer with a
  real multi-step diagnosis graph. The graph: `prepare_context →
  diagnose (LLM + tools: read_file / grep_repo / view_log_excerpt) →
  [optional cross_check (second LLM) → REJECT loops back] → finalize`,
  bounded by `max_steps`. Supports three providers via BYO API key:
  - `anthropic` (`ANTHROPIC_API_KEY`)
  - `openai` (`OPENAI_API_KEY`)
  - `google` (`GOOGLE_API_KEY`)

  Cross-check can mix providers (e.g. Claude diagnoses, GPT validates,
  Gemini cross-checks). Missing API keys surface a clean error and
  fall back to the restart_policy safety net — no stack traces.

- **Healer-runtime billing matters.** Three runtimes now exist with
  three billing models, and 0.10.0 makes the choice explicit:
  - `dispatch.mode: session` — **free** under the user's Claude Code
    subscription. Preferred for Claude Code users.
  - `healing.claude.mode: interactive` — **free** (same session). Legacy.
  - `healing.claude.mode: subprocess` — **per-call** against the
    Anthropic API. Now soft-deprecated (see Notes).
  - `healing.claude.mode: langgraph` — **per-call** against your
    chosen provider. New in 0.10.0; the headless path of choice.

  The `/autosentry` skill body now documents this matrix and steers
  Claude Code users to `dispatch.mode: session` explicitly.

### Changed

- **Install size grew.** 0.10.0 adds `langgraph`, `langchain-core`,
  `langchain-anthropic`, `langchain-openai`, and
  `langchain-google-genai` as hard deps (~50 MB installed, mostly from
  google's gRPC transitives). If this hurts you, file an issue —
  moving the LangGraph stack to an `autosentry[langgraph]` extras_require
  is a non-breaking change we'll do in 0.11.0 if there's demand.

### Deprecated

- **`healing.claude.mode: subprocess`.** Logs a one-line nudge at
  config-load directing users to `dispatch.mode: session` (free) or
  `healing.claude.mode: langgraph` (multi-step + provider choice).
  The legacy path stays functional in 0.10.x — no migration required
  today. Removal is not currently planned but may happen in 1.0.

## [0.9.0] — 2026-05-31

### Added

- **Session-dispatch mode** — the interactive Claude Code session can
  now be the watcher AND the dispatcher for autosentry, not just a
  downstream healer. Originally the monitor was the master loop and the
  session reacted to `recovery_request.md`; now you can flip the roles
  so the session keeps a backstop monitor on autosentry itself and runs
  the heal logic via the Task tool. Opt in with:

  ```yaml
  dispatch:
    mode: session   # default: builtin (unchanged)
  ```

  Four moving parts ship together:

  1. **Detector-only monitor.** Under `dispatch.mode: session` the
     monitor still detects, writes incident folders, and updates the
     heartbeat — but it no longer invokes the rule healer or Claude
     healer, and no longer applies actions itself. The `restart_policy`
     safety net (auto-restart a dead child) still runs so the supervised
     process stays alive when no session is around to react. Every new
     incident touches `.autosentry/session_dispatch_request` so the
     session knows to wake up.

  2. **New `autosentry probe` CLI.** One-shot liveness + pending-work
     check. Stdout is structured JSON (`monitor.pid_alive`,
     `monitor.stale`, `pending_incidents[]` with each entry's
     `rule_match` and `suggested_action`). `--inject-prompt` emits a
     Claude Code Stop-hook `decision: block` payload that re-engages the
     session when there's work. `--advance-cursor <id>` advances the
     pending-incidents cursor.

  3. **`autosentry init` wires the Stop hook.** Init now writes a Stop
     hook into `.claude/settings.local.json` (`autosentry probe
     --inject-prompt --quiet`). Idempotent — re-running init won't
     duplicate it; pre-existing hooks are preserved. New `autosentry
     hooks install` / `autosentry hooks remove` commands manage the
     hook outside of init.

  4. **`autosentry session apply` + monitor action queue.** The session
     enqueues actions via `autosentry session apply --incident <id>
     --action <kind> [--set K=V] [--rule <name>]`; the monitor drains
     the queue (`.autosentry/session_actions.jsonl`) on each tick and
     calls `supervisor.apply_action` for each new entry, advancing
     `.autosentry/session_action_cursor`. Keeps process-management
     invariants inside the monitor where they belong.

- **`/autosentry` skill extended** with a Phase 0 (session-watch fired)
  block. The skill knows how to: probe autosentry, restart the monitor
  if it's down, walk each pending incident, dispatch via either rule
  match or Task subagent, and advance the cursor. The auto-healing
  decision matrix is documented inline.

### Notes

- The legacy `claude.mode: interactive` request/response file dance
  (`recovery_request.md` ↔ `recovery_response.md`) still works under
  the default `dispatch.mode: builtin`. It is **superseded** by session
  dispatch for new setups and will be removed in a future release. No
  action required for existing users.

- The Stop hook is Claude Code only for now. Codex, Gemini, Cursor, and
  Aider users keep the Phase 5 handshake unchanged; hook variants for
  other tools are tracked as a follow-up.

## [0.8.5] — 2026-05-30

### Fixed

- **Monitor no longer sits idle forever after a supervised child exits cleanly**
  (issue #5, Bug 1). When `corpus-forge ingest --once` exited 0 after a 12h
  run, the supervisor stayed alive for another 9 hours doing nothing — the
  default `exit_code` detector correctly treated code 0 as not-an-anomaly but
  the monitor had no exit transition for "supervised work complete." Added a
  `process.lifecycle` knob with three modes:
  - `restart_on_failure` (**new default**): a clean exit (code 0) ends the
    supervisor; non-zero still routes through the healer / restart_policy
    path the same as before.
  - `one_shot`: any exit ends the supervisor.
  - `restart_always`: the pre-0.8.5 behavior — both clean and dirty exits
    route through the healer. Opt in only if you genuinely want loop-forever
    semantics.

  The supervisor now propagates the child's exit code to the CLI, so a
  parent service manager sees the real result instead of always seeing 0.

- **State-save error path no longer hot-loops** (issue #5, Bug 2). After the
  silent idle period in Bug 1, autosentry hit `state save failed: [Errno 2]`
  on the atomic rename (an iCloud sync evictor was racing the
  `state.json.tmp` → `state.json` rename) and emitted **15,978,646** copies
  of that line in 24 minutes. Two changes:
  - `StateStore.save` now falls back to a direct (non-atomic) write when
    `os.replace` raises `FileNotFoundError`, so the save still lands.
  - `Monitor._save_state` wraps repeated failures with capped exponential
    backoff (0.5s → 60s) and per-message dedup. The first failure logs once;
    subsequent calls inside the backoff window are dropped and counted; a
    recovery log line reports how many writes were suppressed.

## [0.8.4] — 2026-05-29

### Fixed

- **Supervisor no longer wheel-spins at ~90% CPU after a Claude healer
  timeout with no matching rule** (issue #4). When the child exited
  non-zero, no rule matched the `exit_code` detector, and the Claude
  healer timed out without applying a fix, the recovery state machine had
  a missing transition: it sat in a tight loop with no restart, no exit,
  and no state update — looking alive to `ps` while the supervised job
  had been dead for hours. The "no action applied" path now falls back to
  the configured `restart_policy` budget: restart the child up to
  `max_restarts` times with `cooldown_seconds` between attempts, then
  stop the monitor cleanly so a service manager (systemd / launchd /
  supervisord) can decide what to do. Anomaly detections on a *live*
  child are unaffected — the fallback only triggers when the child is
  actually dead. Includes notify hooks (`recovery` and `exit`) and clears
  `state.last_exit_code` after a fallback restart so the next exit
  re-fires the detector path as a fresh transition.

## [0.8.3] — 2026-05-27

### Changed

- **Agent playbooks now direct operating agents to background long-running
  work in interactive sessions.** `AGENTS.md` and `.autosentry/program.md`
  tell the agent to start the monitor with the `nohup` pattern and spawn
  recovery/diagnosis subagents *in the background* — letting the runtime
  notify on completion — so the interactive chat stays free, reserving the
  foreground for steps whose output is needed immediately.

## [0.8.2] — 2026-05-27

### Fixed

- **`autosentry update` misdetected uv-tool (and pipx) installs as `pip`
  and failed with `No module named pip`.** `detect_install_method()`
  resolved `sys.executable` before scanning it, but uv/pipx tool venvs
  symlink `bin/python` to a base interpreter *outside* the tool tree — so
  resolving erased the `uv/tools` / `pipx/venvs` marker and the `~/.local`
  heuristic fell through to `pip`, which a tool venv has no `pip` to run.
  Detection now scans the **unresolved** `sys.executable` and `sys.prefix`,
  so uv installs correctly route to `uv tool upgrade autosentry`. As
  defense in depth, the pip upgrade path now checks that `python -m pip` is
  actually available and falls back to `install.sh` if not, instead of
  hard-failing.

## [0.8.1] — 2026-05-27

### Added

- **New `/autosentry-update` skill.** A focused slash command that runs
  `autosentry update --check` and applies the right upgrade backend
  (uv / pipx / pip / Homebrew) when you're behind — the update counterpart
  to `/autosentry-init`. Install with `autosentry skills install --skill
  update` (or `--skill all` for all three). Ships for Claude, OpenCode,
  Codex, Gemini, Cursor, and Zed; `--skill all` now covers `autosentry`,
  `init`, and `update`.

## [0.8.0] — 2026-05-27

### Changed — config moved into `.autosentry/`

- **The config now lives at `.autosentry/autosentry.yaml`.** Everything
  autosentry writes — config, state, logs, incidents, prompts — sits
  under `.autosentry/`, so a single `.autosentry/` entry git-ignores the
  lot. `autosentry init` writes the config there and drops a
  `.autosentry/.gitignore` (`*`) that keeps the whole tree out of version
  control by default (delete it to track the config). Every command's
  `--config` now defaults to `.autosentry/autosentry.yaml`.
- **Relative config paths resolve against the project root** — the
  directory containing `.autosentry/` — instead of the config file's own
  directory. `state_path`, `incidents_dir`, `log_dir`, `config_snapshots`,
  and `process.cwd` are unaffected by the move; they still anchor on the
  repo root.
- **Backward compatible.** A pre-0.8 root-level `autosentry.yaml` is still
  loaded as a fallback, so existing repos keep working untouched.
  `autosentry init --upgrade` migrates a legacy root config into
  `.autosentry/` in place (comments preserved). A fresh `init` that finds
  a legacy config refuses to silently shadow it — it points you at
  `--upgrade` (migrate) or `--force` (start clean).

### Removed

- **Dropped the dead `src/autosentry/cli.py` god-module.** It was shadowed
  by the `autosentry.cli` package and never imported; the live CLI is the
  per-command tree under `autosentry/cli/commands/`.

## [0.7.4] — 2026-05-27

### Added — agent-driven update detection

- **`autosentry update --check` now caches the PyPI lookup** for a day
  (`~/.cache/autosentry`, XDG-aware; `--no-cache` forces a live query) and
  **exits 0** whether or not you're behind, printing a recommendation
  (`→ update available — run autosentry update`) when there's a newer
  release. This lets the `/autosentry` skill run it on every invocation
  without hammering PyPI or aborting triage chains.
- **`autosentry update --json`** emits `{"current","latest","is_outdated"}`
  for scripts and agents that prefer to parse the result.
- **Homebrew installs are detected.** `update` recognizes a Cellar-based
  install and runs `brew upgrade autosentry` (and recommends that command)
  instead of falling back to `install.sh`.
- **Skill playbooks now nudge updates.** `AGENTS.md` and the per-tool
  `/autosentry` wrappers (Claude, Cursor, Codex, OpenCode, Zed, Gemini)
  run the version check during triage and recommend the upgrade command
  when one is available, without upgrading unprompted.

### Changed — default posture is agent-first

- **`escalate_to_claude_after` default lowered** from `max_restarts // 2`
  (5) to `max(1, max_restarts // 5)` (2 with `max_restarts=10`). Two
  unverified rule-driven restarts and the agent takes over — rules are
  the cheap fast lane, the agent is the main fix path.
- **New `healing.escalate_on_rule_regression` flag** (default `True`).
  When a rule-based fix regresses inside the verify window, the next
  attempt for that detector skips rules and routes straight to the
  agent. Rules already failed on that detector; recycling them is
  wasted budget.
- **README reframed** around the agentic flow as the headline feature:
  rules are an accelerator, not the centerpiece. The "best-effort for
  novel bugs" caveat is replaced with the structural safeguards that
  back the agent path — fix branches, outcome verification, attempts
  ledger.

### Fixed

- **Interactive `autosentry init` no longer loses your input on a
  terminal left in raw / no-echo mode.** When a prior program (a crashed
  TUI, an interrupted pager, a dropped `ssh`) exits without restoring its
  termios, the next prompt inherited the broken state: typed characters
  weren't echoed (input appeared lost), backspace did nothing, and the
  line never reached the program — so `init` silently fell back to the
  suggested default instead of the command you typed. `ask()`/`confirm()`
  now load `readline` and additively repair the terminal to a sane cooked
  mode (`ICANON`/`ECHO`/`ECHOE`/…) before prompting. Best-effort and
  POSIX-only; a no-op on Windows and non-TTYs, and it never clears
  unrelated termios bits.

## [0.7.3] — 2026-05-27

### Fixed

- **`autosentry --version` now reports the real installed version.**
  `__version__` was a hardcoded string in `src/autosentry/__init__.py`
  that drifted from `pyproject.toml` after the 0.7.2 release — the
  wheel was 0.7.2 on PyPI but the banner still said `v0.7.1`. The
  constant is now resolved at import time via
  `importlib.metadata.version("autosentry")`, with a sentinel fallback
  for source-tree imports without an installed dist. Single source of
  truth = `pyproject.toml`.
- **Sync stale "self-healing sentry" taglines to "self-healing
  supervisor"** across the CLI banner, Typer `--help` text, package
  docstrings, and the README hero. The 0.7.2 PyPI-summary rewrite
  didn't propagate to the in-repo duplicates.

## [0.7.2] — 2026-05-26

### Changed

- **PyPI summary rewritten** to match the README hero voice:
  *"Self-healing supervisor for long-running processes — watch a
  command, catch the failure, fix it, leave a paper trail."* The
  previous one-liner read like a feature list.

### Fixed

- **`Detector._last_fired_at` no longer false-positives the cooldown on
  the very first observation.** Initializing to `0.0` meant the first
  call to `observe_line` was considered on-cooldown whenever
  `time.monotonic()` (boot-relative, so small on fresh CI runners) was
  less than `cooldown_seconds`. Now initialized to `-inf` so a
  never-fired detector is correctly off-cooldown.
- **`test_init_install_skills_local_drops_files`** strips ANSI SGR
  codes before its substring assertion. CI sets `FORCE_COLOR=1`, which
  makes Rich emit color escapes that split the asserted string.

## [0.7.1] — 2026-05-26

### CI / Release

- **`.github/workflows/ci.yml` reworked**: split a fast `lint + typecheck`
  gate from the test matrix; added Python **3.13** to the matrix; added
  a `build sdist + wheel` job that runs `twine check` so packaging
  breakage is caught at PR time. Switched lint/format/typecheck steps to
  named single-purpose stages and added `workflow_dispatch` so the
  workflow can be re-run from the Actions tab.
- **`.github/workflows/pre-commit.yml`** — new workflow that runs every
  hook in `.pre-commit-config.yaml` against the full tree on push/PR,
  with hook caching.
- **`.github/workflows/release.yml`** — new tag-driven workflow:
  builds sdist+wheel, runs `twine check`, then publishes to PyPI via
  **Trusted Publishing (OIDC)** so no API token has to live in repo
  secrets.
- All GitHub Actions bumped to current majors:
  `actions/checkout@v5`, `astral-sh/setup-uv@v5`,
  `actions/upload-artifact@v4`, `actions/download-artifact@v4`,
  `actions/cache@v4`, `actions/setup-python@v5`.
- README badge row expanded: **CI** · **pre-commit** · **PyPI** ·
  **Python versions** · **Ruff** · **pyrefly** · **License**.

### Fixed

- `pyproject.toml` version field was stuck at `0.6.1` (drifted from
  `__init__.py`); both now agree on `0.7.1`.
- Added `Programming Language :: Python :: 3.13` classifier to match
  the CI matrix.

### Interactive init UX

The interactive `autosentry init` flow now offers to install the
`/autosentry` slash command at either scope — local or global —
right inside the same prompt sequence as `process.command` and
`config_snapshots`. The banner also renders to set the tone of an
interactive setup session.

### Added

- **`--install-skills local|global|none|ask`** on `autosentry init`.
  Default is `ask`: prompt in interactive sessions, fall back to
  `none` when stdin/stdout aren't TTYs (so scripts don't silently
  install globally). `local` and `global` skip the prompt and apply
  directly — useful from `autosentry init --for-agent --install-skills local`.
- The interactive prompt shows the destination paths inline so the
  user sees what they're about to write before saying yes:
  `~/.claude/commands/autosentry.md`, `~/.codex/prompts/autosentry.md`,
  etc. for global; `.claude/commands/autosentry.md` etc. for local.
- ASCII banner now renders at the start of interactive init (skipped
  when stdout is piped, `--non-interactive` is set, or `--for-agent`
  is set so plain-text consumers don't get the art).
- The next-steps summary at the end of init is wrapped in a rich
  Panel so it visually detaches from the prompts and skills-install
  output above it.

### Changed

- `_print_next_steps` switched from line-by-line printing to a single
  `Panel` render. Colors auto-strip in non-TTY contexts so the panel
  still works in CI logs.

## [0.7.0] — 2026-05-26

Skills get a new dimension: install **globally** (every repo gets the
slash command) or **locally** (just this repo), and pick between the
full `/autosentry` playbook or the focused `/autosentry-init` setup
skill. Plus pre-commit hooks for the project itself.

### Added

- **`autosentry skills install --scope local|global`** — global writes
  into each tool's home-directory location (`~/.claude/commands/`,
  `~/.codex/prompts/`, etc.) so every interactive session inherits
  the skill without re-running `skills install` per repo. Local
  remains the default.
- **`autosentry skills install --skill autosentry|init|all`** — the
  new `init` skill is a focused `/autosentry-init` slash command for
  onboarding a fresh repo, separate from the full `/autosentry`
  operator playbook. `--skill all` installs both.
- **Per-tool init wrappers**: `claude_init.md`, `opencode_init.md`,
  `codex_init.md`, `gemini_init.toml`, `cursor_init.md`, `zed_init.md`,
  plus the canonical `init.md`. Aider / Continue / Windsurf use their
  ambient-context files, which already point at AGENTS.md.
- **Pre-commit hooks** — `.pre-commit-config.yaml` runs `ruff check
  --fix`, `ruff format`, and `pyrefly check src/autosentry` on every
  `git commit`. Auto-installs via `make install` when `pre-commit` is
  on PATH. Manual: `make hooks` or `pre-commit install`. Documented in
  CONTRIBUTING.md, including the `--no-verify` opt-out.
- **`autosentry skills list`** now shows both local and global
  destinations per (tool, skill) pair. A blank global cell means the
  tool has no global slot (`agents` / AGENTS.md is repo-specific).

### Changed

- `SkillTarget` dataclass gains `skill` (`"autosentry" | "init"`) and
  `global_destination: Path | None` fields. `destination` survives as
  a read-only alias for `local_destination` so any external code that
  referenced the old name continues to work.
- `skills.install()` signature gains `skill_name` and `scope`
  keyword arguments. Defaults preserve the historical behavior
  (`skill_name="autosentry"`, `scope="local"`).
- `pre-commit>=3.7` added to `[project.optional-dependencies].dev`.

## [0.6.1] — 2026-05-26

The restart budget is now healer-aware: verified fixes don't burn
budget, and the healer is forced to engage *before* the give-up
threshold instead of after.

### Added

- **`state.restarts_total`** — all-time restart counter, never resets.
  Audit-only, surfaces in `autosentry doctor` and the ledger.
- **`healing.escalate_to_claude_after`** — when ``state.restarts``
  (the unverified counter) reaches this value, the next detection
  skips the YAML rule healer and goes straight to Claude. Default:
  ``max_restarts // 2``.
- **Escalation notification** — high-visibility one-shot when the
  flag flips on, so operators see "forcing Claude diagnosis" before
  the give-up email.
- **`autosentry doctor` healer-budget row** — surfaces both the
  escalation threshold and the give-up threshold, plus current
  unverified + all-time counters.

### Changed

- **`state.restarts` resets to 0 on any kept verification.** The
  give-up check (`state.restarts >= state.max_restarts`) now means
  "consecutive unverified restarts," not "all-time restarts." A
  successful heal returns the supervisor to a healthy state and gives
  subsequent failures a fresh budget — matches the user-facing
  intent that "new runs after heal don't count."
- ``MonitorState.record_restart`` increments both counters; only
  ``restarts`` resets on kept.
- The give-up message logs both counters so an operator can tell
  the difference between "5 unverified in a row" and "57 total over
  the deploy's lifetime."

## [0.6.0] — 2026-05-26

The Claude healer becomes a first-class peer of an open Claude Code
session. Instead of always spawning a headless `claude --print`
subprocess, the healer can write a file-handshake request that the
`/autosentry` skill picks up and routes to a **subagent** (via the
Task tool) for diagnosis. The user's main session stays clean; the
subagent gets the full incident context in its own.

### Added

- **Interactive healer mode.** `healing.claude.mode: interactive`
  writes `.autosentry/recovery_request.md` with YAML frontmatter
  (`incident_id`, `detector`, `subagent.type`, `timeout_seconds`) and
  blocks waiting for `.autosentry/recovery_response.md`. The
  `/autosentry` skill spawns the right subagent via the Task tool and
  produces the response by running `autosentry healer respond`.
- **`autosentry healer respond` CLI.** Lets a subagent produce the
  response file with one Bash call:
  `autosentry healer respond --action restart_with_env --set BATCH_SIZE=4
  --diagnosis "OOM at step 8450"`. Atomic write (tmp + rename) so the
  healer's mtime check sees a complete file.
- **Subagent routing config.** `healing.claude.subagents` maps detector
  names → `SubagentSpec(type, description)` with a `default` fallback.
  Per-detector specialization (e.g. `training_stall` → Plan agent,
  `oom` → general-purpose).
- **`healing.claude.enabled: auto`** (new default). Auto-resolves
  based on what's actually available.
- **`healing.claude.mode: auto`** (new default). Resolves to
  `interactive` when a `/autosentry` skill is installed,
  `subprocess` when `claude` is on PATH, and **disabled** (rule-only)
  when neither is available — no more red `doctor` rows for users
  without the Claude CLI.

### Changed

- **`autosentry doctor`** now reports the resolved healer mode
  instead of treating "no `claude` on PATH" as an automatic failure.
  Rows like `auto → interactive (/autosentry skill installed)`,
  `auto → subprocess (claude on PATH)`, or
  `auto → rule-only (no skill, no CLI)` (warn).
- **`/autosentry` skill template** grows Phase 5 — the highest-
  priority phase. The skill now explicitly instructs Claude to spawn
  a subagent (not diagnose inline) when a recovery request is open.
  Updates to both `autosentry.md` (canonical) and `AGENTS.md`
  (universal).
- `ClaudeHealer.attempt` dispatches to `_subprocess_attempt` or
  `_interactive_attempt` based on `_resolve_mode()`. Backward
  compatible — `mode: subprocess` is still a first-class option for
  headless deployments.

## [0.5.1] — 2026-05-26

A CLI polish release. The Typer entry point is now a small package
instead of a 725-line god-file, and the operator experience is much
more legible.

### Added

- **`autosentry doctor`** — environment health check. Verifies the
  CLI is on PATH, git is available, the cwd is a repo, `autosentry.yaml`
  parses, tree-sitter grammars load for declared languages, and the
  Claude CLI is reachable when healing is enabled. Renders a rich
  table with ok/warn/fail rows and exits non-zero only on fail rows.
- **`autosentry onboard`** — phase-aware setup walkthrough. Auto-
  detects whether the repo is uninstalled / not initialized / not
  running / running and prints the right next step. Outputs plain
  bullets (no ANSI) when stdout is not a TTY OR `--for-agent` is
  passed — easy for AI agents to consume.
- **`autosentry init --for-agent`** — drops a structured
  `.autosentry/AGENT_NOTES.md` next to the config so an AI editor
  has a per-repo onboarding cheat sheet alongside the universal
  `AGENTS.md`.
- **Interactive `autosentry init`**. When stdin/stdout are TTYs and
  `--non-interactive` isn't passed: detects the user's stack
  (pyproject.toml / package.json / Cargo.toml / go.mod / Gemfile)
  and suggests `process.command`; offers to add detected config
  files (`.env`, `configs/*.yaml`, `pyproject.toml`, etc.) to
  `config_snapshots`. YAML rewrite goes through ruamel so the
  heavily-commented template survives intact.
- **Rich banner** on `autosentry --version` — small ASCII hero with
  version and tagline.
- **`autosentry/cli/style.py`** module: shared Console, color tokens
  (`OK`/`WARN`/`ERR`/`INFO`/`DIM`/`ACCENT`), TTY-aware ANSI stripping,
  `banner()`, `kv_panel()`, and thin `ask`/`confirm` prompt wrappers
  that auto-return defaults in non-interactive sessions. Respects
  `NO_COLOR`, `FORCE_COLOR`, and `CLICOLOR_FORCE`.

### Changed

- **`cli.py` → `cli/` package** with one module per command:
  `init`, `run`, `watch`, `web`, `status`, `analyze`, `incidents`,
  `dispatcher`, `skills`, `update`, `doctor`, `onboard`. The top-
  level `autosentry.cli:app` entry point is unchanged.
- Color usage across the CLI is now consistent: success → green,
  warnings → yellow, errors → red, identifiers → cyan, hints → dim.
- `autosentry init` prints a colored "next steps" panel instead of
  three bare lines.
- The CHANGELOG header style for sub-patches uses an italic blurb on
  top of the heading; mirrors what bigger projects do.

## [0.5.0] — 2026-05-26

### Added

- **Discord integration.** Two new dispatcher backends —
  `DiscordWebhookBackend` (outbound only via Discord incoming webhook)
  and `DiscordBotBackend` (bidirectional via Discord HTTP API v10).
  `discord_bot` opens a thread under the parent message on first
  delivery and routes subsequent messages into that thread channel,
  matching the way Slack's thread_ts model works inside autosentry's
  dispatcher state.
- **Inbound Discord polling.** Same command grammar as Slack: `abort`,
  `pause`, `resume`, `set <key> <value>`, `approve`, `comment: …`.
  Filters bot messages (its own and other bots') from the inbox so
  humans-only commands land in `discord_inbox.jsonl`.
- **`discord_outbox` notifier.** Mirrors `slack_outbox` exactly — same
  wire format, same file-based indirection. Both notifiers now inherit
  from a shared `FileOutboxNotifier` base.
- **Per-platform dispatcher state files.** When running a Discord
  dispatcher, the inbox/state/marker default paths automatically
  switch to `discord_*.json[l]` so Slack and Discord daemons can run
  side by side without colliding.
- **CLI**: new flags on `autosentry dispatcher run` —
  `--discord-webhook-url` (env `DISCORD_WEBHOOK_URL`),
  `--discord-token` (env `DISCORD_BOT_TOKEN`),
  `--discord-bot-user-id` (env `DISCORD_BOT_USER_ID`).
  `--backend` accepts `discord_webhook` and `discord_bot`.
- **Four new AI-editor skill wrappers**: `aider`, `continue`,
  `windsurf`, `zed`. `autosentry skills install --tool <name>` drops
  the right file at the right path:

  | tool      | path                              |
  |-----------|-----------------------------------|
  | aider     | `.aider.conf.yml`                 |
  | continue  | `.continue/config.json`           |
  | windsurf  | `.windsurfrules`                  |
  | zed       | `.zed/prompts/autosentry.md`      |

  All defer to `AGENTS.md`; the universal AGENTS.md is now the
  authoritative playbook.

### Changed

- **All skill files refreshed.** The Phase 2 skills predated `watch`,
  `web`, `analyze`, fix branches, the attempts ledger, the Slack
  inbox commands, and `program.md`. AGENTS.md and the canonical
  `autosentry.md` are now full Phase-4-aware playbooks; per-tool
  wrappers (Claude / OpenCode / Codex / Gemini / Cursor) are
  slimmed-down routers that defer to AGENTS.md.
- **`_detect_backend`** signature changed to keyword-only with
  per-platform args. Now selects in order: `slack_api` →
  `discord_bot` → `webhook` (Slack) → `discord_webhook` → `stdout`.
- **Notifier refactor.** `SlackOutboxNotifier` now subclasses
  `FileOutboxNotifier`. Behavior unchanged; identical wire format.
- New `discord_outbox` value accepted by `NotifierSpec.kind`.

## [0.4.0] — 2026-05-26

Inspired by [autoresearch](https://github.com/ulmentflam/autoresearch) —
each fix attempt now lives on its own git branch, gets outcome-verified
by watching for the same detector to re-fire, and is recorded in a flat
`attempts.tsv` ledger that mirrors autoresearch's `results.tsv`.

### Added

- **Fix-branch isolation.** When the Claude healer fires, autosentry
  creates a fresh branch `autosentry/fix-<incident-id>`, lands the
  edits there, and verifies before merging. If the same detector
  re-fires inside `healing.verify_window_seconds` (default 600s), the
  fix is treated as a regression: the working tree is restored and the
  branch is left behind as a forensic artifact. Auto-merge of verified
  fixes into `main` is opt-in via `healing.git.auto_merge`.
- **`attempts.tsv` ledger.** Append-only flat TSV at
  `.autosentry/attempts.tsv` with one row per fix attempt: timestamp,
  incident_id, detector, source (rule name or `claude`), branch,
  status (`pending` / `kept` / `discarded` / `crashed` / `regressed`),
  duration_seconds, description. Status updates are atomic full-file
  rewrites.
- **Outcome verification.** After applying a fix, the monitor watches
  for the same detector for `verify_window_seconds` before declaring
  the attempt `kept`. Regressions trigger the configured
  `healing.regression_action` (`revert` / `escalate` / `ignore`).
- **Healer budget.** Per-detector rolling window
  (`healing.budget.max_attempts_per_detector_per_hour`, default 5).
  When exhausted, autosentry stops attempting fixes for that detector
  until a manual `approve` lands in the Slack inbox or the window
  passes — but still writes incidents and notifies.
- **`autosentry analyze`** CLI command. Reads `attempts.tsv` and
  prints: top failing detectors, per-rule success rate, regression
  streaks. Flags: `--since 24h|30m|7d|600s`, `--json`.
- **`program.md`** template, scaffolded into `.autosentry/program.md`
  by `autosentry init`. The "operator mission statement" inspired by
  autoresearch's `program.md`. Codifies the autonomous loop the agent
  should follow: read the ledger, triage regressions, propose new
  rules when the same detector keeps escalating to Claude.

### Changed

- `HealingConfig` gains `git`, `budget`, `verify_window_seconds`, and
  `regression_action` fields. All have safe defaults; existing
  `autosentry.yaml` files continue to work without changes.
- Automated commits made by autosentry use `--no-gpg-sign` to avoid
  prompting the user's signer (e.g. 1Password) during unattended
  operation. Sign the final merge commit by hand.

## [0.3.1] — 2026-05-26

### Changed

- **Dispatcher daemon is now lazy.** Outbox drain is mtime-gated — idle
  iterations cost one ``stat`` call instead of a full file read +
  rewrite. Inbound Slack polling fires only when an inbound marker file
  mtime advances, or after ``idle_inbound_seconds`` (default 300s) as a
  long-period safety sweep. Default loop cadence raised from 5s to 30s
  because the loop itself is so cheap now.
- The monitor touches ``.autosentry/inbox_poll_request`` on every
  detection fire, tying Slack reply polling to the anomaly-detection
  cycle (the natural "something just happened" beat).

### Added

- **Monitor-side inbox consumer** (``autosentry.inbox``): reads
  ``slack_inbox.jsonl`` on each monitor tick and applies recognized
  commands directly. Cheap (local file IO only) and runs alongside the
  existing detector pass. Recognized commands:
  - ``abort`` — stop the supervisor and shut down the monitor
  - ``pause`` — stop the supervisor; keep the monitor alive
  - ``resume`` — start the supervisor if it isn't running
  - ``set max_restarts N`` — update ``state.max_restarts`` live
  - ``set <key> <value>`` — write into ``state.user[set_<key>]`` for
    rules to read
  - ``approve`` — recorded as a placeholder for the future
    Claude-approval hook
  - ``comment: <text>`` — appended to ``state.user["comments"]``
- ``state.user["last_processed_inbox_id"]`` tracks consumption so
  commands aren't re-applied across monitor restarts.
- Dispatcher CLI: ``--inbound-marker`` (default
  ``.autosentry/inbox_poll_request``) and ``--idle-inbound-seconds``
  (default 300s). Set ``--idle-inbound-seconds 0`` to rely entirely on
  the marker.

## [0.3.0] — 2026-05-26

### Added

- **SLURM supervisor.** Submits jobs via `sbatch`, parses the job id from
  stdout, polls `squeue` / `sacct` for status, tails the SLURM log file
  (handles delayed creation + rotation), and re-submits on restart. All
  SLURM-specific commands (`status_command`, `cancel_command`,
  `sacct_command`) are overridable in `process.extra`.
- **Docker supervisor.** Runs `docker run` (typically `-d`), streams
  `docker logs -f`, and stops + removes + re-runs on restart. The
  container name is the single source of truth and is auto-generated as
  `autosentry-<8 hex>` if not configured.
- **Attach supervisor.** Observe-only: points at an existing PID and log
  file. `restart` actions are rejected; `abort` sends SIGTERM only when
  `extra.allow_kill: true`. Tails the log file with rotation handling.
- **Bidirectional Slack dispatcher daemon.** `autosentry dispatcher run`
  now actually drains the outbox AND polls thread replies. Three
  backends:
  - `stdout` (MCP / dev mode — prints; no network)
  - `webhook` (Slack incoming webhook URL, outbound only)
  - `slack_api` (Slack Web API with `SLACK_BOT_TOKEN`, supports threads
    AND inbound polling via `conversations.replies`)
  Inbound human replies are appended to `slack_inbox.jsonl` with parsed
  commands: `abort`, `pause`, `resume`, `approve`, `set <key> <value>`,
  and `comment: <free text>`. Persists state at
  `.autosentry/dispatcher_state.json` (thread map + last-seen reply per
  thread for de-dup across restarts).
- **`autosentry watch`** — live status TUI built on `rich.Live`. Panels:
  state snapshot (pid, uptime, restarts, last heartbeat), recent
  incidents, per-detector "last fired" rows, and a colorized log tail.
  Refresh interval configurable; `--once` for a single-frame render.
- **`autosentry web`** — read-only HTTP incident viewer. Stdlib
  `http.server` only; no FastAPI/Flask dep. Index page lists incidents
  with a client-side filter; detail page renders `report.md` and links
  to the raw artifacts (trace, frames, configs, fix). Defaults to
  `127.0.0.1:8765`. Warns if bound to `0.0.0.0`. Path-traversal
  defended.

### Changed

- `autosentry dispatcher` is now a sub-typer with `run` and `pending`
  subcommands. The old behavior is now `autosentry dispatcher pending`.
- `ProcessConfig.env` and `ProcessConfig.extra` tolerate YAML's empty-
  key-as-null behavior (`extra:` with no children is coerced to `{}`).
- New runtime dep: `markdown-it-py` (already a transitive of `rich`,
  pinned explicitly so the web viewer's Markdown renderer is reliable).

## [0.2.0] — 2026-05-26

### Added

- **Licensing.** Apache 2.0 `LICENSE` and `NOTICE`; pyproject metadata
  updated accordingly.
- **`install.sh`** — one-liner installer at the repo root. Detects `uv`,
  `pipx`, and `pip --user` (in that order) and dispatches to whichever is
  available. Honors `AUTOSENTRY_VERSION`, `AUTOSENTRY_PRE`,
  `AUTOSENTRY_METHOD`, `AUTOSENTRY_PYTHON`, `AUTOSENTRY_QUIET`, and
  `NO_COLOR`. Idempotent; safe to re-run as an upgrade-in-place.
- **`autosentry update`** CLI subcommand. Auto-detects the install
  backend, runs the matching upgrade command, and falls back to
  re-running `install.sh` from GitHub when the backend can't be
  determined. Flags: `--check`, `--pre`, `--version`, `--method`.
- **`update.sh`** — standalone updater. Delegates to `autosentry update`
  when the CLI is on PATH; otherwise pipes the canonical `install.sh`
  back into `sh`.
- **`autosentry skills install`** — drops `/autosentry` slash-command
  wrappers into a user's repo for the supported AI tools. Flags:
  `--tool` (repeatable; values `claude`, `opencode`, `codex`, `gemini`,
  `cursor`, `agents`, or `all`), `--target`, `--force`.
- **`autosentry skills list`** — shows the tools and destinations the
  installer knows about.
- **Per-tool skill templates.** `claude.md`, `opencode.md`, `codex.md`,
  `gemini.toml`, `cursor.md` ship inside the package, plus a universal
  `AGENTS.md` (the convention now adopted by Codex, Gemini, Cursor, and
  OpenCode).
- **Polished README** with badges, table of contents, install/update
  one-liners, an anatomy-of-an-incident walkthrough, configuration
  reference, comparison vs supervisord/systemd/k8s/runit/tini, and an
  FAQ.
- **OSS health files.** `CONTRIBUTING.md`. (Code of conduct, security
  policy, and PR/issue templates are tracked for follow-up.)

### Changed

- `pyproject.toml` license field now uses SPDX form (`license =
  "Apache-2.0"`) with `license-files = ["LICENSE", "NOTICE"]`.

### Notes

- The package isn't published to PyPI yet, so `autosentry update --check`
  will return 404 until the first release artifact lands there. The
  `install.sh` falls back to source installs in that environment.

## [0.1.0] — 2026-05-26

### Added

- Initial implementation, generalized from the
  `qwerky-distill/slurm/rad_monitor.py` ML pipeline supervisor.
- **Local subprocess supervisor** (`process.kind: local`). SLURM /
  Docker / Attach are scaffolded with explicit `NotImplementedError` and
  share the same `Supervisor` protocol.
- **Detectors.** `pattern` (regex), `traceback` (Python / Node / Go /
  Rust / Java), `stall` (no-progress or no-output with optional
  `metric_regex`), and `exit_code`.
- **YAML rule engine.** Detector → action mapping with `restart`,
  `restart_with_env` (`half` / `double` / literal), `pause`, `abort`,
  and `custom_command`.
- **Claude CLI fallback healer.** Invoked when no rule matches; reads
  state, last incident report, and config snapshots; captures any
  edits Claude makes as a `fix/diff.patch`.
- **Incident store.** Folder-per-incident layout under
  `.autosentry/incidents/<ts>-<kind>/` with `report.md`, exploded source
  frames (tree-sitter framing across six languages), `trace.txt`,
  `log_excerpt.txt`, `configs/*`, `state.json`, `rule_match.json`, and
  `fix/`. Append-only `index.jsonl`.
- **Notifiers.** `log` (default), `slack_outbox` (file-based queue
  drained by a separate dispatcher session), and `webhook`.
- **CLI.** `autosentry init`, `autosentry run`, `autosentry status`,
  `autosentry incidents list|show`, `autosentry dispatcher`.
- **Tooling.** Makefile, GitHub Actions CI (Python 3.10–3.12 × Ubuntu /
  macOS, ruff lint + format check, pyrefly typecheck, pytest +
  coverage). iCloud `UF_HIDDEN` workaround baked into `make install`.

[Unreleased]: https://github.com/ulmentflam/autosentry/compare/v0.8.4...HEAD
[0.8.4]: https://github.com/ulmentflam/autosentry/releases/tag/v0.8.4
[0.8.3]: https://github.com/ulmentflam/autosentry/releases/tag/v0.8.3
[0.8.2]: https://github.com/ulmentflam/autosentry/releases/tag/v0.8.2
[0.8.1]: https://github.com/ulmentflam/autosentry/releases/tag/v0.8.1
[0.8.0]: https://github.com/ulmentflam/autosentry/releases/tag/v0.8.0
[0.7.4]: https://github.com/ulmentflam/autosentry/releases/tag/v0.7.4
[0.7.1]: https://github.com/ulmentflam/autosentry/releases/tag/v0.7.1
[0.7.0]: https://github.com/ulmentflam/autosentry/releases/tag/v0.7.0
[0.6.1]: https://github.com/ulmentflam/autosentry/releases/tag/v0.6.1
[0.6.0]: https://github.com/ulmentflam/autosentry/releases/tag/v0.6.0
[0.5.1]: https://github.com/ulmentflam/autosentry/releases/tag/v0.5.1
[0.5.0]: https://github.com/ulmentflam/autosentry/releases/tag/v0.5.0
[0.4.0]: https://github.com/ulmentflam/autosentry/releases/tag/v0.4.0
[0.3.1]: https://github.com/ulmentflam/autosentry/releases/tag/v0.3.1
[0.3.0]: https://github.com/ulmentflam/autosentry/releases/tag/v0.3.0
[0.2.0]: https://github.com/ulmentflam/autosentry/releases/tag/v0.2.0
[0.1.0]: https://github.com/ulmentflam/autosentry/releases/tag/v0.1.0
