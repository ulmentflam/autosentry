<div align="center">

# autosentry

**Self-healing sentry for long-running processes.**

Watch a command, catch the failure, fix it, leave a paper trail.

[![CI](https://github.com/ulmentflam/autosentry/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ulmentflam/autosentry/actions/workflows/ci.yml)
[![pre-commit](https://github.com/ulmentflam/autosentry/actions/workflows/pre-commit.yml/badge.svg?branch=main)](https://github.com/ulmentflam/autosentry/actions/workflows/pre-commit.yml)
[![PyPI](https://img.shields.io/pypi/v/autosentry.svg)](https://pypi.org/project/autosentry/)
[![Python](https://img.shields.io/pypi/pyversions/autosentry.svg)](https://pypi.org/project/autosentry/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![pyrefly](https://img.shields.io/badge/typed-pyrefly-blueviolet.svg)](https://github.com/facebook/pyrefly)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)

</div>

`autosentry` supervises a long-running command — an ML training run, a data
pipeline, a service that's expected to stay up — watches its log stream for
known failure modes and anomalies, applies deterministic recovery rules when
it knows the answer, and escalates to a Claude Code shell when it doesn't.
Every incident is written into `.autosentry/incidents/` as a folder
containing the exploded source around the failure, a stack trace, snapshots
of the configs that were in effect, and the fix that was applied.

It was generalized from a domain-specific monitor (`rad_monitor.py`) that
ran a multi-stage ML pipeline end-to-end and auto-healed it through repeated
failures. The shape — file-based state, file-based Slack outbox, blocking
escalation to Claude — is intentionally simple and observable.

---

## Contents

- [Why autosentry](#why-autosentry)
- [Install](#install)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Anatomy of an incident](#anatomy-of-an-incident)
- [Configuration reference](#configuration-reference)
- [Detectors](#detectors)
- [Recovery rules and Claude fallback](#recovery-rules-and-claude-fallback)
- [Notifications](#notifications)
- [Launching from your AI editor](#launching-from-your-ai-editor)
- [One-shot install via an AI agent](#one-shot-install-via-an-ai-agent)
- [Update](#update)
- [Status & roadmap](#status--roadmap)
- [Comparison](#comparison)
- [FAQ](#faq)
- [Contributing](#contributing)
- [License](#license)

---

## Why autosentry

Long-running jobs fail in three flavors:

1. **Known transient failures** — NCCL hiccups, connection resets, OOMs that
   would clear with a smaller batch. A static `restart-on-failure` policy
   plus a few env-var nudges is enough.
2. **Anomalies** — training stalls, loss spikes, throughput drops. There's
   no exception to catch; you need someone watching the metrics.
3. **Novel failures** — code bugs, config mistakes, library regressions.
   Restarting doesn't help; somebody has to read the trace and edit a file.

Most supervisors handle (1). Almost nothing handles (3) without a human in
the chair. autosentry handles all three: deterministic YAML rules for the
boring cases, an LLM (Claude Code by default) for the open-ended ones, and
a structured incident store so a real human can audit either path later.

---

## Install

### One-line (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh
```

The installer detects `uv`, `pipx`, or `pip` (in that order) and uses the
best one available. Pin a specific version with `AUTOSENTRY_VERSION=0.2.0`.

### From PyPI

```bash
uv add autosentry           # uv
pipx install autosentry     # pipx (isolated)
pip install autosentry      # plain pip
```

### From source

```bash
git clone https://github.com/ulmentflam/autosentry.git
cd autosentry
make install
```

> **macOS / iCloud Drive:** if your clone lives under
> `~/Library/Mobile Documents/`, iCloud sets `UF_HIDDEN` on `_*.pth` files
> inside any venv and Python's `site.py` then skips them, breaking editable
> installs. The `Makefile` detects this and points the venv at
> `~/.cache/autosentry-venv` automatically. Override with
> `make install VENV=/path/to/venv`.

### Let an AI agent do it

If you're already in a Claude Code / Cursor / Codex / Aider / OpenCode /
Windsurf / Zed / Continue / Gemini session, paste the prompt block below
and let the agent install and configure autosentry for the repo you're
sitting in. It's a short, declarative brief — the agent runs the right
commands for your stack, asks before destructive actions, and leaves
the repo in a state where `autosentry run` works on the next try.

```
Install and set up autosentry in this repo.

Follow this order; stop and ask me before doing anything that would
overwrite an existing file or change tracked code.

1. Verify autosentry isn't already installed. If it isn't, install it
   with the one-liner from the README:
       curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh
   Then confirm with `autosentry --version`.

2. Run `autosentry init --non-interactive` to scaffold autosentry.yaml
   and the .autosentry/ tree. (Use `--upgrade --force` if a config
   already exists and looks pre-0.6.1.)

3. Inspect this repo to figure out:
     - the right `process.command` (the thing I want supervised — read
       pyproject.toml / package.json / Cargo.toml / go.mod / the
       Makefile / scripts/ to guess; ASK ME before settling on it)
     - which files belong in `config_snapshots` (env files, run configs,
       pipeline definitions)
     - a starting set of detectors and rules tailored to my stack
       (OOM / NCCL / connection-reset patterns for ML; HTTP 5xx /
       connection-refused for web; stall regex matching whatever
       progress format my process emits)
   Edit autosentry.yaml in place.

4. Install the /autosentry slash command for me with
   `autosentry skills install --tool all`. This drops AGENTS.md plus the
   per-tool wrappers so future sessions get the playbook automatically.

5. Run `autosentry doctor`. If anything is red, fix it. If it's all
   green or only warnings, summarize the warnings.

6. Tell me the exact command to start the monitor in the background
   (the `nohup autosentry run …` one-liner), but DO NOT run it
   yourself. I'll start it.

Be terse. One or two sentences per step. Point me at autosentry.yaml
and `.autosentry/program.md` for context — don't re-narrate the docs.
```

---

## Quick start

```bash
cd my-project
autosentry init                  # interactive: detects your stack, asks for process.command
autosentry doctor                # verifies the env is healthy
autosentry run                   # starts monitoring
```

`autosentry init` is interactive when run from a real terminal — it
detects whether your repo is python/node/go/rust, suggests a starter
`process.command`, and offers to snapshot config files. Pass
`--non-interactive` to skip prompts (CI-friendly), or `--for-agent`
to drop a `.autosentry/AGENT_NOTES.md` cheat sheet alongside the
config.

For AI agents stepping into a repo for the first time:

```bash
autosentry onboard --for-agent    # plain-text, phase-aware playbook
```

Tail the structured log in another shell:

```bash
tail -F .autosentry/logs/autosentry.log
```

Live operator dashboard:

```bash
autosentry watch                # rich TUI: state, incidents, log tail
autosentry web                  # browse incidents in your browser
```

Browse incidents from the CLI:

```bash
autosentry incidents list
autosentry incidents show 2026-05-26T14-32-10Z-error-traceback
```

Bidirectional Slack:

```bash
# In a separate shell. Reads .autosentry/slack_outbox.jsonl, posts to Slack,
# polls the thread for human replies, appends them to slack_inbox.jsonl.
SLACK_BOT_TOKEN=xoxb-… autosentry dispatcher run --channel C0A4UK987ND
```

Check current state:

```bash
autosentry status
```

---

## How it works

```
   ┌─────────────────────────────────────────────────────────────────┐
   │                       autosentry monitor                        │
   │  start ──► tick loop: read log lines → run detectors → fire     │
   │            healers → apply action → write incident → notify     │
   └─────────────────────────────────────────────────────────────────┘
        │                  │                 │                │
        ▼                  ▼                 ▼                ▼
   Supervisor        Detectors          Healers          Notifiers
   local / slurm /   pattern /          rules.yaml  →    log /
   docker / attach   traceback /        Claude CLI       slack outbox /
                     stall /            (fallback)       webhook
                     exit_code
                             │                                 │
                             ▼                                 ▼
                  ┌──────────────────────────┐    ┌──────────────────────┐
                  │  Incident store          │    │ Slack dispatcher     │
                  │  .autosentry/incidents/  │    │ outbox → Slack       │
                  │    <ts>-<kind>/          │    │ Slack thread → inbox │
                  │      report.md           │    │ (abort/pause/set…)   │
                  │      trace.txt           │    └──────────────────────┘
                  │      frames/*.md         │
                  │      configs/*           │    ┌──────────────────────┐
                  │      state.json          │ ◄──│ autosentry watch     │
                  │      fix/                │ ◄──│ autosentry web       │
                  └──────────────────────────┘    └──────────────────────┘
```

Layers are deliberately small and pluggable:

| layer          | role                                                    | built-in implementations                     |
|----------------|---------------------------------------------------------|----------------------------------------------|
| Supervisor     | start, observe, restart the process                     | `local`, `slurm`, `docker`, `attach`         |
| Detector       | watch the log stream and process state for anomalies    | `pattern`, `traceback`, `stall`, `exit_code` |
| Healer         | decide what to do about a detection                     | YAML rules → Claude CLI fallback             |
| Incident store | persist a forensic record of what happened + the fix    | folder-per-incident with `index.jsonl`       |
| Notifier       | broadcast events                                        | `log`, `slack_outbox`, `webhook`             |
| Dispatcher     | bidirectional Slack bridge (outbound + thread inbound)  | `stdout`, `webhook`, `slack_api`             |
| Visualization  | operator surfaces                                       | `autosentry watch`, `autosentry web`         |

The monitor's main loop is a single, synchronous Python thread that pulls
log lines off the supervisor's queue. No async, no callbacks across
processes. If you can read [`monitor.py`](./src/autosentry/monitor.py),
you can debug anything autosentry does.

---

## Anatomy of an incident

A `.autosentry/incidents/<ts>-<kind>/` folder looks like this:

```
2026-05-26T14-32-10Z-error-traceback/
├── report.md              ← human-readable, the marquee artifact
├── trace.txt              ← raw stack trace
├── log_excerpt.txt        ← ±200 lines around the failure
├── frames/
│   ├── 01-train.py.md     ← exploded source for frame 1 (function + ±10 lines)
│   ├── 02-loader.py.md
│   └── 03-_torch_dist.py.md  ← library frame, source explode skipped
├── configs/
│   ├── run.yaml           ← snapshot of each declared config file
│   └── .env
├── state.json             ← monitor state at the moment of the incident
├── rule_match.json        ← which YAML rule fired (or "claude")
└── fix/
    ├── action.json        ← {"kind":"restart_with_env","env":{"BATCH_SIZE":"4"}}
    ├── diff.patch         ← if Claude edited files, the diff lives here
    └── claude_response.md ← Claude's diagnosis text (if invoked)
```

A real `report.md` for an OOM:

```markdown
# Incident — 2026-05-26 14:32:10 UTC — error / traceback

**Process:** local · `python train.py --config configs/run.yaml`
**PID:** 41822 · **Restart #:** 2/5
**Detector:** `traceback` (Python)
**Resolution:** rule `oom` → restart_with_env (BATCH_SIZE=4)

---

## Source — frame 1
`src/train.py:142` in `TrainLoop.step()`

```python
class TrainLoop:
    def step(self, batch):
        self.optimizer.zero_grad()
>>>     logits = self.model(batch["input_ids"])   # line 142
        loss = self.criterion(logits, batch["labels"])
        loss.backward()
```

## Stack trace
```
torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.34 GiB
```

## Fix applied
Rule `oom` matched. Restarted with `BATCH_SIZE=4` (was `8`).
```

Anomalies (stall, loss spike, etc.) get the same shape — the trace section
is replaced by a recent-metrics block and the configs section is expanded
into a "decisions that might be relevant" view.

---

## Configuration reference

After `autosentry init` you have an `autosentry.yaml` with every option
commented in place. The top-level shape:

| key                | type          | default                          | what it does |
|--------------------|---------------|----------------------------------|--------------|
| `process.kind`     | enum          | `local`                          | `local` only in Phase 1; `slurm`/`docker`/`attach` in Phase 3 |
| `process.command`  | list[str]     | —                                | argv passed to the process. No shell interpretation. |
| `process.cwd`      | str           | `.`                              | working dir, relative to the config file |
| `process.env`      | dict[str,str] | `{}`                             | env vars; values can interpolate `$VAR` / `${VAR}` |
| `process.restart_policy.max_restarts` | int | `5`                  | when exceeded, monitor gives up |
| `process.restart_policy.cooldown_seconds` | int | `60`             | wait before restart |
| `monitor.poll_interval_seconds` | int | `30`                       | tick rate for status checks and tick-driven detectors |
| `monitor.log_dir`  | str           | `.autosentry/logs`               | structured log + supervised process log live here |
| `monitor.log_excerpt_lines` | int  | `200`                            | lines per incident `log_excerpt.txt` |
| `config_snapshots` | list[str]     | `[]`                             | files copied verbatim into every incident folder |
| `source_explode.context_lines` | int | `10`                          | lines around hot line when AST framing fails |
| `source_explode.languages` | list | `[python, javascript, typescript, go, rust, java]` | tree-sitter grammars to load |
| `source_explode.skip_paths` | list | site-packages / node_modules / etc.      | frames in these paths emit "library" stubs |
| `detectors`        | list          | see below                        | what to watch for |
| `rules`            | list          | `[]`                             | YAML rule engine; first match wins |
| `healing.claude.enabled` | bool    | `true`                           | fall back to Claude CLI if no rule matches |
| `healing.claude.command` | list    | `["claude", "--print"]`          | how to invoke Claude |
| `healing.claude.timeout_seconds` | int | `600`                       | Claude's budget per incident |
| `notifiers`        | list          | `[{kind: log}]`                  | event sinks |
| `state_path`       | str           | `.autosentry/state.json`         | persistent state location |
| `incidents_dir`    | str           | `.autosentry/incidents`          | where incident folders go |

---

## Detectors

| kind          | what it does                                                                                          |
|---------------|-------------------------------------------------------------------------------------------------------|
| `pattern`     | Fires when a log line matches a regex. Cheapest, most common.                                         |
| `traceback`   | Picks up multi-line stack traces from **Python**, **Node/JS**, **Go**, **Rust**, **Java**.            |
| `stall`       | With `metric_regex`: progress value doesn't advance for N seconds → anomaly. Without: no log output for N seconds → anomaly. |
| `exit_code`   | Process exits non-zero (or zero too, if you flip `nonzero_only: false`).                              |

Example detector block:

```yaml
detectors:
  - kind: pattern
    name: oom
    regex: "(OutOfMemoryError|CUDA out of memory)"
  - kind: pattern
    name: nccl
    regex: "NCCL.*(error|timeout)"
  - kind: traceback
  - kind: stall
    name: training_stall
    metric_regex: "step (\\d+)/"
    no_progress_seconds: 1800
  - kind: exit_code
```

---

## Fix branches and outcome verification

Inspired by [autoresearch](https://github.com/ulmentflam/autoresearch):
every Claude-driven fix attempt runs on its own branch and is **verified**
before it sticks.

1. When the Claude healer fires, autosentry creates
   `autosentry/fix-<incident-id>` off the current HEAD.
2. Claude's edits land on that branch. The supervisor is restarted.
3. The monitor watches for the same detector to re-fire within
   `healing.verify_window_seconds` (default 600s).
4. **No recurrence →** the attempt is marked `kept` in `attempts.tsv`.
   With `healing.git.auto_merge: true` the branch fast-forwards into
   your working branch and is deleted; otherwise the branch is left
   for you to merge by hand.
5. **Recurrence inside the window →** the attempt is marked
   `regressed`. The working tree is restored, you're returned to your
   original branch, and the fix branch stays put as a forensic
   artifact.

Every attempt is recorded in `.autosentry/attempts.tsv` — flat
tab-separated, append-only, grep-friendly. Browse it with
`autosentry analyze`:

```bash
$ autosentry analyze --since 24h

attempts — 14 total (last 24h)
kept=9  pending=1  regressed=3  crashed=1

           top failing detectors
┃ detector          ┃ attempts ┃
┃ training_stall    ┃        6 ┃
┃ oom               ┃        4 ┃
┃ nccl              ┃        3 ┃

                      per-rule success
┃ source             ┃ total ┃ kept ┃ regressed ┃ success ┃
┃ oom_halve_batch    ┃     4 ┃    3 ┃         1 ┃     75% ┃
┃ stall_restart      ┃     6 ┃    3 ┃         2 ┃     60% ┃
┃ claude             ┃     3 ┃    3 ┃         0 ┃    100% ┃
```

The **healer budget**
(`healing.budget.max_attempts_per_detector_per_hour`, default 5) caps
how many times autosentry will attempt to fix the same detector in a
rolling hour. When it burns through, the monitor still writes incidents
and notifies — but stops trying fixes for that detector until a manual
`approve` lands in the Slack inbox.

## Recovery rules and Claude fallback

Rules are tried top-down; the first one whose `match` clause is satisfied by
a detection wins. Anything unmatched falls through to the Claude healer,
which runs in one of two modes:

### Healer modes

- **subprocess** — spawn `claude --print` as a headless process,
  pipe the prompt in, capture stdout. Works without an open Claude
  Code session; needs the `claude` CLI on PATH. Use in CI, k8s, on
  air-gapped boxes.
- **interactive** — write a recovery request file with YAML
  frontmatter (incident id, detector, recommended subagent type),
  then block waiting for a response. The `/autosentry` slash command
  running in your open Claude Code session sees the request, **spawns
  a subagent via the Task tool** with the incident's full context, and
  writes the response (typically via `autosentry healer respond`).
  Keeps your main session clean; the subagent owns the diagnosis.

```yaml
healing:
  claude:
    enabled: auto           # auto-detect; never red-light the doctor
    mode: auto              # interactive if skill installed, else subprocess
    subagents:
      default:
        type: general-purpose
        description: "Diagnose an autosentry incident"
      training_stall:
        type: Plan          # specialize per detector
        description: "Diagnose a stalled training loop"
```

Mode resolution (when `mode: auto`):

| `/autosentry` skill installed | `claude` on PATH | resolved mode |
|---|---|---|
| ✅ | – | `interactive` |
| ❌ | ✅ | `subprocess` |
| ❌ | ❌ | disabled (rule-only — no red doctor row) |

`autosentry doctor` reports the resolved mode so you can see which one
will actually run.

### Subagents

In interactive mode the healer doesn't talk to Claude directly — it
prompts the `/autosentry` skill (running in the user's open session) to
spawn a **Task-tool subagent** of the type declared in the request
frontmatter. That subagent reads the incident folder, edits the repo
if needed, and writes the response file with one Bash call:

```bash
autosentry healer respond \
  --action restart_with_env \
  --set BATCH_SIZE=4 \
  --diagnosis "OOM at step 8450; halving batch."
```

Per-detector subagent routing (`healing.claude.subagents`) lets each
failure mode get the right kind of investigator without inflating the
operator's main conversation.

Rule-only operation still works exactly as before — set
`healing.claude.enabled: false` and autosentry never tries Claude.

```yaml
rules:
  - name: oom_halve_batch
    match: { detector: oom }
    action:
      kind: restart_with_env
      set:
        BATCH_SIZE: half          # halves the prior overlay
      notify: true

  - name: transient_restart
    match: { detector: nccl }
    action: { kind: restart, notify: true }

  - name: stall_restart
    match: { detector: training_stall }
    action: { kind: restart, notify: true }
```

Supported actions: `restart`, `restart_with_env` (with `half`/`double`/literal
values in `set:`), `pause`, `abort`, `custom_command`.

When Claude is invoked, it reads:

- the recovery prompt template at `.autosentry/prompts/recovery.md`,
- the current `state.json`,
- the last incident report (so it has the exploded source frames),
- snapshots of every file listed in `config_snapshots`.

It is expected to (a) optionally edit files in place — those edits are
captured into `fix/diff.patch` — and (b) end its response with an `ACTION:`
block:

```
ACTION: restart_with_env
SET: BATCH_SIZE=4
```

If Claude says `ACTION: abort`, the monitor stops and waits for a human.

---

## Notifications

Notifier specs are a list under `notifiers:`. Built-ins:

```yaml
notifiers:
  - kind: log                                # always-on default
  - kind: slack_outbox
    outbox_path: .autosentry/slack_outbox.jsonl
    channel: "C0A4UK987ND"                    # Slack channel id
    thread_key: "pipeline"
  - kind: discord_outbox
    outbox_path: .autosentry/discord_outbox.jsonl
    channel: "123456789012345678"             # Discord channel id (snowflake)
    thread_key: "pipeline"
  - kind: webhook
    url: "https://hooks.example.com/autosentry"
```

Neither notifier talks to chat directly — they append JSON lines to an
outbox file. A separate `autosentry dispatcher run` daemon drains the
outbox and (with the `slack_api` or `discord_bot` backend) also polls
the thread for replies. The dispatcher is *lazy*:

- **Outbox drain is mtime-gated** — when nothing has been queued, the
  dispatcher's loop costs one `stat()` call.
- **Inbound polling is trigger-driven** — the monitor `touch()`es
  `.autosentry/inbox_poll_request` on every detection fire, which is
  what wakes the dispatcher's Slack-thread poll. A long-period sweep
  (`--idle-inbound-seconds 300`) catches replies sent during quiet
  stretches.

This indirection mirrors the original `rad_monitor.py` and lets
autosentry run on machines without outbound network. The monitor
consumes `slack_inbox.jsonl` / `discord_inbox.jsonl` on its own tick
and applies recognized commands (`abort`, `pause`, `resume`,
`set max_restarts N`, `approve`, `comment:`) directly to the
supervised process.

Pick the backend with env vars or `--backend`:

| credentials present                     | backend chosen      | inbound? |
|-----------------------------------------|---------------------|----------|
| `SLACK_BOT_TOKEN`                       | `slack_api`         | ✅       |
| `DISCORD_BOT_TOKEN`                     | `discord_bot`       | ✅       |
| `SLACK_WEBHOOK_URL`                     | `webhook`           | ❌       |
| `DISCORD_WEBHOOK_URL`                   | `discord_webhook`   | ❌       |
| (none)                                  | `stdout`            | ❌       |

Run a Slack daemon and a Discord daemon side by side — the dispatcher
auto-namespaces its state/inbox/marker files per platform.

---

## Launching from your AI editor

`autosentry skills install` drops a `/autosentry` slash-command into
your repo for whichever AI editor you use. Once it's there, typing
`/autosentry` asks the agent to bootstrap autosentry, configure it
for your process, or walk you through the last incident — without
leaving your editor.

```bash
# install in just this repo (default)
autosentry skills install                          # all tools, /autosentry skill
autosentry skills install --tool claude            # one tool
autosentry skills install --skill init             # the focused /autosentry-init slash command
autosentry skills install --skill all              # both /autosentry and /autosentry-init

# install once, inherit everywhere
autosentry skills install --scope global           # writes ~/.claude/commands/, ~/.codex/prompts/, ...
autosentry skills install --scope global --skill all

autosentry skills list                             # full destination table (local + global)
```

Two skills land here:

- **`/autosentry`** — full operator playbook (install → init → run → operate → interactive recovery).
- **`/autosentry-init`** — focused onboarding of a fresh repo (no operator/recovery content). Smaller reading cost for AI agents that only have one job.

Supported tools:

| tool                            | dropped at                              | invoke           |
|---------------------------------|-----------------------------------------|------------------|
| **Claude Code**                 | `.claude/commands/autosentry.md`        | `/autosentry`    |
| **OpenCode**                    | `.opencode/command/autosentry.md`       | `/autosentry`    |
| **OpenAI Codex CLI**            | `.codex/prompts/autosentry.md`          | `/autosentry`    |
| **Gemini (Antigravity / CLI)**  | `.gemini/commands/autosentry.toml`      | `/autosentry`    |
| **Cursor**                      | `.cursor/commands/autosentry.md`        | `/autosentry`    |
| **Aider**                       | `.aider.conf.yml` (binds `AGENTS.md`)   | ambient context  |
| **Continue.dev**                | `.continue/config.json`                 | `/autosentry`    |
| **Windsurf (Cascade)**          | `.windsurfrules`                        | ambient context  |
| **Zed**                         | `.zed/prompts/autosentry.md`            | `/autosentry`    |
| Universal (any AGENTS.md-aware) | `AGENTS.md` at the repo root            | auto-loaded      |

All wrappers defer to `AGENTS.md` for the full playbook; the
single-source-of-truth for the agent's instructions lives there.

The skill prompt itself is canonical and lives at
`src/autosentry/templates/skills/autosentry.md` inside this repo. All
per-tool wrappers either embed it or reference it.

---

## One-shot install via an AI agent

For convenience, the same prompt also lives near the top of this README
under [Let an AI agent do it](#let-an-ai-agent-do-it). Copy/paste it
into your editor's session; it handles install → init → detector
proposals → `autosentry skills install` → `autosentry doctor` and asks
before clobbering anything.

If you'd rather start interactively yourself:

```bash
autosentry onboard --for-agent       # plain-text phase-aware playbook
autosentry doctor                    # is the env healthy?
```

## Update

```bash
autosentry update                    # update to latest stable
autosentry update --check            # report current vs latest, no install
autosentry update --pre              # allow pre-releases
```

Or use the standalone updater (works for installs made by `install.sh` even
when the CLI itself is broken):

```bash
curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/update.sh | sh
```

---

## Status & roadmap

| capability                                              | status        |
|---------------------------------------------------------|---------------|
| Local subprocess supervisor                             | **stable**    |
| Pattern / traceback / stall / exit detectors            | **stable**    |
| YAML rule engine + Claude CLI fallback                  | **stable**    |
| Tree-sitter source exploder (py/js/ts/go/rust/java)     | **stable**    |
| Incident store + structured logs + slack file-outbox    | **stable**    |
| `install.sh` one-liner                                  | **stable**    |
| `autosentry update` mechanism                           | **stable**    |
| AI-editor skills (Claude/OpenCode/Codex/Gemini/Cursor)  | **stable**    |
| SLURM supervisor                                        | **stable**    |
| Docker supervisor                                       | **stable**    |
| Attach-to-PID supervisor                                | **stable**    |
| Slack dispatcher daemon (outbound + inbound)            | **stable**    |
| `autosentry watch` status TUI                           | **stable**    |
| `autosentry web` incident viewer                        | **stable**    |
| Fix-branch isolation + outcome verification             | **stable**    |
| `attempts.tsv` ledger + `autosentry analyze`            | **stable**    |
| `program.md` operator mission statement                 | **stable**    |
| PyPI release automation                                 | Phase 5       |
| Slack interactive buttons (approve/abort UI)            | Phase 5       |

---

## Comparison

| tool             | restarts a process | reads logs | rule-based recovery | LLM-driven recovery | incident audit trail |
|------------------|--------------------|------------|---------------------|---------------------|----------------------|
| `supervisord`    | ✅                 | ❌         | ❌                  | ❌                  | log files            |
| `systemd`        | ✅                 | ❌         | ❌                  | ❌                  | journald             |
| `k8s livenessProbe` | ✅              | ❌         | ❌                  | ❌                  | events               |
| `runit`          | ✅                 | ❌         | ❌                  | ❌                  | log files            |
| `tini` / `dumb-init` | partial        | ❌         | ❌                  | ❌                  | none                 |
| **autosentry**   | ✅                 | ✅         | ✅ (YAML)            | ✅ (Claude)          | folder-per-incident  |

autosentry is **not** trying to replace your supervisor of record. It runs
*above* one (or alongside it) and is designed for the long-running,
high-friction-to-restart job: the multi-day training run, the slow nightly
ETL, the batch service that can't easily be turned into a stateless k8s
deployment.

---

## FAQ

**Does autosentry need Claude installed?**
No. With `healing.claude.enabled: false`, autosentry is rules-only. Useful
when you're confident your rule set covers your failure modes, or when
running on a machine without outbound network.

**Does it support pre-existing log files?**
Phase 1 supervises a process it starts itself. Phase 3 adds an `attach`
supervisor that tails an existing log file and watches an existing PID.

**What happens to the process when the monitor itself crashes?**
The supervised process keeps running — autosentry uses
`start_new_session=True` so it doesn't share a process group. On restart,
the monitor reads its persisted state and resumes. If the process is gone
by then, it starts a fresh one.

**Can rules call out to scripts?**
Yes — `action: { kind: custom_command, command: [...] }`. Runs in the
configured `cwd` with the configured env. Returns the action.

**Will Claude actually edit my code?**
By default, yes. It runs in the configured `cwd`. Any file edits it makes
are captured as a diff in the incident folder so you can review and revert.
Set `healing.claude.command` to something more restrictive (e.g.
`["claude", "--print", "--no-edit"]`) if you'd rather Claude only diagnose.

**iCloud Drive keeps breaking my venv.**
See the [install](#install) note. Either point the venv outside iCloud or
let the `Makefile` auto-redirect it.

---

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md). The short version:

```bash
git clone https://github.com/ulmentflam/autosentry.git
cd autosentry
make install
make ci         # ruff lint + format check + pyrefly + pytest
```

Open an issue first for non-trivial changes. New behavior needs a test.

---

## License

Apache 2.0 — see [LICENSE](./LICENSE) and [NOTICE](./NOTICE).
