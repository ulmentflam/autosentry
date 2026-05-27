# Changelog

All notable changes to autosentry are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/ulmentflam/autosentry/compare/v0.7.4...HEAD
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
