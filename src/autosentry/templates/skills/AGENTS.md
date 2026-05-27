# AGENTS.md — autosentry

This file is the canonical instruction set for any AI agent operating
in a repo that uses **autosentry**. It's the format adopted by OpenAI
Codex CLI, Google Gemini (Antigravity), Cursor, OpenCode, and Aider
for project-level agent guidance. Per-tool slash-command wrappers
under `.claude/commands/`, `.opencode/command/`, `.codex/prompts/`,
`.gemini/commands/`, `.cursor/commands/`, `.windsurfrules`,
`.continue/config.json`, and `.zed/prompts/` all defer to this file.

## What autosentry is

A self-healing supervisor for long-running processes. The monitor
watches one command (an ML training run, a service, an ETL job), reads
its log stream and process state, applies deterministic YAML rules
when it knows the failure mode, and escalates to an LLM (Claude Code
by default) when it doesn't. Each event becomes a folder under
`.autosentry/incidents/`. Each fix attempt becomes a row in
`.autosentry/attempts.tsv`. Claude-driven fixes land on their own
branch (`autosentry/fix-<incident-id>`) and only stick if they
**verify** — the same detector doesn't re-fire inside the verify
window.

Repository: <https://github.com/ulmentflam/autosentry>.

## Your job as the agent

When the user invokes `/autosentry` (or asks about autosentry without
a slash), determine which **phase** they're in and help them progress.

```bash
command -v autosentry >/dev/null && autosentry --version       # phase ≥ 1
[ -f autosentry.yaml ] && echo configured                      # phase ≥ 2
[ -f .autosentry/state.json ] && cat .autosentry/state.json    # phase ≥ 3
# phase 4 = phase 3 + the pid in state.json is alive
ls -la .autosentry/recovery_request.md 2>/dev/null              # phase 5?
```

**Phase 5 is the highest-priority.** When the monitor escalates a
detection in interactive mode it writes
`.autosentry/recovery_request.md` and blocks waiting for
`.autosentry/recovery_response.md`. Handle that *before* anything else.

## Phase 5 — recovery request open

When the request file is newer than the response file:

1. Read the request. YAML frontmatter tells you the incident id, the
   detector, and the recommended subagent type.
2. **Spawn a subagent via the Task tool** with `subagent_type` matching
   the frontmatter's `subagent.type` (default `general-purpose`). Pass
   the request body as the subagent's prompt, prefaced with: *"You were
   spawned by autosentry to diagnose a recovery request. Read
   `.autosentry/incidents/<incident_id>/report.md` for full context.
   When done, run `autosentry healer respond --action <kind> [--set …]
   --diagnosis "<summary>"`."*
3. Wait for the subagent. It produces the response file itself.
4. Verify `.autosentry/recovery_response.md` exists with an `ACTION:`
   line. The monitor consumes it on the next tick (~1s).
5. Summarize for the user: detector, action, diff location.

Do not diagnose inline. The subagent is the isolated context.

## Phase 1 — install

```bash
curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh
```

The installer prefers `uv tool install`, falls back to `pipx`, then
`pip --user`. Validate afterward with `autosentry --version`. Offline
fallback: `pip install --user autosentry`.

## Phase 2 — initialize and configure

`autosentry init` writes `autosentry.yaml` (heavily commented),
`.autosentry/program.md` (the operator mission statement), and a
`.autosentry/` skeleton. Then walk the user through:

- **`process.kind`** — `local`, `slurm`, `docker`, or `attach`.
- **`process.command`** — argv list to supervise. No shell syntax.
- **`process.env`** — env vars; `$VAR` / `${VAR}` interpolation supported.
- **`process.restart_policy.max_restarts`** — the give-up threshold.
- **`config_snapshots`** — files copied into every incident folder.
  Include the run config, the `.env`, and any pipeline definition.
- **`detectors`** — start with `pattern` regexes for known failure
  modes (OOM, NCCL, connection reset), a `traceback` detector, a
  `stall` with `metric_regex` matching the user's progress format, and
  `exit_code`.
- **`rules`** — pair each detector to an action. Tried in order, first
  match wins; everything else falls through to Claude.
- **`healing.git.auto_merge`** — leave `false` (default) so the user
  manually merges verified fix branches. Set `true` only if the agent
  is trusted to land code unattended.

When configuring detectors, *read the user's code* to propose patterns
that match their actual stack — don't guess from the config name alone.

## Phase 3 — launch

```bash
# Foreground (good for first run; you see everything land in real time)
autosentry run

# Background (long-running supervision)
nohup autosentry run > /dev/null 2>&1 &
tail -F .autosentry/logs/autosentry.log
```

If a detector fires immediately, walk the user through the resulting
`.autosentry/incidents/<id>/report.md`. If nothing fires for several
minutes and the user expected something, either the detectors don't
match the log format (read the log and adjust regexes) or
`monitor.poll_interval_seconds` is too long.

## Phase 4 — operate

```bash
autosentry status                    # snapshot of state.json
autosentry watch                     # live TUI: state + incidents + log tail
autosentry web                       # browse incidents in a browser
autosentry incidents list            # last N incidents
autosentry incidents show <id>       # full report.md
autosentry analyze --since 24h       # ledger summary
autosentry update --check            # is there a newer release?
```

### Reading an incident

When the user asks "why did it restart?", open the latest incident
folder. The `report.md` has the exploded stack frames and the rule
that fired; `fix/` has the action JSON and any Claude diff. The
matching row in `attempts.tsv` shows whether the fix verified.

### Fix branches

Claude-driven fixes land on a branch named
`autosentry/fix-<incident-id>`. The monitor watches for the same
detector for `healing.verify_window_seconds` (default 600s):

- **kept** — verification passed. With `auto_merge: true` the branch
  is fast-forwarded into the user's working branch and deleted. With
  `auto_merge: false` (default), the branch is left for the operator
  to merge by hand.
- **regressed** — same detector re-fired inside the window. The
  working tree is restored, the branch is left behind as a forensic
  artifact, and (depending on `healing.regression_action`) the
  detector may be paused.

You can list these branches at any time:

```bash
git branch --list 'autosentry/fix-*'
```

### Slack inbox commands

If the user runs `autosentry dispatcher run` with the `slack_api`
backend, replies to the incident thread are parsed and acted on:

| Slack message       | effect                                              |
|---------------------|-----------------------------------------------------|
| `abort`             | stop the supervisor + shut down the monitor         |
| `pause`             | stop the supervisor; keep the monitor alive         |
| `resume`            | start the supervisor                                |
| `set max_restarts N`| update `state.max_restarts` live                    |
| `set <key> <value>` | write into `state.user[set_<key>]` for rules to read|
| `approve`           | recorded as a placeholder for future approval hook  |
| `comment: <text>`   | appended to `state.user["comments"]`                |

The dispatcher is lazy by design — it polls Slack only when the
monitor's anomaly-detection cycle touches the marker file, plus a
long-period sweep (default 300s) as a safety net.

### The attempts ledger

`.autosentry/attempts.tsv` is the audit trail. Columns:

```
timestamp  incident_id  detector  source  branch  status  duration_seconds  description
```

`status` values: `pending` (verification in flight), `kept` (verified),
`discarded`, `crashed`, `regressed`.

`autosentry analyze` summarizes the ledger:

- top failing detectors (windowed by `--since`)
- per-rule success rate
- detector regression streaks

`--json` for machine reading. Run periodically; propose new YAML
rules whenever the same detector has escalated to Claude 3+ times
with a similar fix pattern.

### High-leverage moves

If you see three or more incidents for the same detector that all
show `source: claude` in `attempts.tsv` (no rule fired) and the fixes
all `kept`, propose a YAML rule that codifies the recurring fix:

```yaml
- name: descriptive_name
  match: { detector: <detector_name>, message_regex: "..." }
  action: { kind: restart_with_env, set: { KEY: value } }
```

Insert it under `rules:` before any catch-all. Every codified rule
turns a slow Claude call into a deterministic restart.

If you see a `regressed` streak ≥ 2 for the same detector, the budget
will eventually pause it. Diagnose by reading the latest incident +
the branch left by the regressed fix:

```bash
git switch autosentry/fix-<incident-id>
# inspect the changes Claude made; figure out why they didn't help
git switch -
```

## program.md

`autosentry init` drops `.autosentry/program.md` — the operator
mission statement. It codifies the autonomous loop you're expected to
follow: read the ledger, triage regressions, propose new rules, never
stop the loop without human approval. If a user customized that file,
prefer their version over the guidance here when they conflict.

## Designing detectors

Work from a recent incident's `log_excerpt.txt`. Extract the line that
uniquely identifies the failure mode (prefer the verb of what broke,
not the language-specific exception class).

For *anomalies* (no exception, but something is wrong), prefer `stall`
with a `metric_regex` over a heuristic `pattern`. Stalls are the most
common silent failure mode in long-running jobs.

## Bounds

- Don't start `autosentry run` in the foreground from a
  non-interactive context — it won't terminate. Use the background
  pattern.
- Don't modify `.autosentry/state.json` or `.autosentry/attempts.tsv`
  directly; the monitor and dispatcher own them.
- Don't merge `autosentry/fix-*` branches that haven't verified
  cleanly. Verification is the truth signal; trust the ledger.
- When editing the user's repo to fix a recurring issue, leave a
  small, reviewable diff. autosentry's Claude integration captures
  diffs into the incident folder; honor that workflow.
- Don't suggest brittle workarounds (like swallowing exceptions or
  disabling detectors) instead of root-cause fixes.

## Known failure modes

- **macOS / iCloud Drive**: Python venvs inside iCloud-synced
  directories get `UF_HIDDEN` on `_*.pth` files, breaking editable
  installs. Either point the venv outside iCloud
  (`UV_PROJECT_ENVIRONMENT=$HOME/.venvs/x`) or use autosentry's
  project Makefile, which auto-redirects.
- **`autosentry update --check` returns 404**: the package isn't on
  the registry yet, or the network is blocked. Install from source as
  a fallback.
- **No detection fires** when something obviously broke: usually the
  log format doesn't match the configured regexes. Read the actual
  logs in `.autosentry/logs/process.log` and adjust.
- **Git signing prompts during a fix**: autosentry uses
  `--no-gpg-sign` for automated commits to avoid stalling on 1Password
  / SSH-key prompts. Sign the final merge commit by hand when keeping
  a fix.

## Style

Be terse. One or two sentences per action. The structured log, the
incident folders, and the attempts ledger are the user's actual
interface — point at them; don't re-narrate them.
