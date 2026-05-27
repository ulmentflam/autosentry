# /autosentry

You are helping the user run **autosentry** — a self-healing supervisor
for long-running processes. The monitor watches one command, applies
deterministic YAML rules when it knows the failure, and (in interactive
mode) escalates to **you** when it doesn't. Repo:
<https://github.com/ulmentflam/autosentry>.

## Your job

When the user invokes `/autosentry`, figure out which phase they're in
and help them progress. Phases:

1. **Not installed** — `autosentry` is not on PATH.
2. **Installed, not initialized** — no `autosentry.yaml` in the cwd.
3. **Initialized, not running** — config exists; monitor isn't started.
4. **Running** — monitor is up; user wants status / incidents / rules.
5. **Recovery request open** — the monitor has written
   `.autosentry/recovery_request.md` and is blocked waiting for a
   response. **This is the highest-priority phase.**

```bash
command -v autosentry >/dev/null && autosentry --version       # ≥ phase 1
[ -f autosentry.yaml ] && echo configured                      # ≥ phase 2
[ -f .autosentry/state.json ] && cat .autosentry/state.json    # ≥ phase 3
ls -la .autosentry/recovery_request.md 2>/dev/null             # phase 5?
```

## Phase 5 — recovery request open (handle this first)

When `.autosentry/recovery_request.md` exists AND its mtime is newer
than `.autosentry/recovery_response.md` (or the response file is
absent), the monitor is **blocked on you**.

**You MUST spawn a subagent via the Task tool to diagnose.** Do not
diagnose inline. The user's session stays clean; the subagent gets the
full incident context in its own.

1. Read `.autosentry/recovery_request.md`. The YAML frontmatter tells
   you the incident id, detector, and the recommended subagent type:

   ```yaml
   ---
   incident_id: 2026-05-26T14-32-10Z-error-traceback
   detector: training_stall
   subagent:
     type: general-purpose
     description: Diagnose a stalled training loop
   timeout_seconds: 600
   ---
   ```

2. **Spawn the subagent.** Use the Task tool with `subagent_type` set
   to the request's `subagent.type`. Pass the entire request body
   (frontmatter + prompt) as the subagent's prompt, prepended with:

   > You were spawned by autosentry to diagnose a recovery request.
   > Read `.autosentry/incidents/<incident_id>/report.md` for the
   > exploded stack frames, configs, and trace. Edit files as needed.
   > When you have a fix, run **exactly one** command:
   >
   > ```bash
   > autosentry healer respond --action <kind> [--set KEY=VALUE]... \
   >   --diagnosis "<one-line summary>"
   > ```
   >
   > Valid actions: `restart`, `restart_with_env`, `pause`, `abort`,
   > `custom_command`. Use `restart_with_env` with `--set BATCH_SIZE=…`
   > when the fix is an env-var change; `restart` when you edited
   > files in place; `abort` when you can't diagnose safely.

3. **Wait for the subagent to finish.** When it returns, the response
   file should already exist on disk (the subagent runs
   `autosentry healer respond` itself). If the subagent finished
   without writing the response file, run the command yourself with
   the subagent's recommendation.

4. **Verify.** Confirm `.autosentry/recovery_response.md` exists and
   contains an `ACTION:` line. The monitor will pick it up within one
   poll interval (~1s).

5. Report a one-line summary to the user: which detector fired, what
   action the subagent chose, and where the diff landed (if any).

## Phase 1 — install

```bash
curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh
```

Validate with `autosentry --version`. Offline fallback:
`pip install --user autosentry`.

## Phase 2 — initialize and configure

```bash
autosentry init             # interactive (suggests command for your stack)
autosentry doctor           # confirm the env is healthy
```

Walk the user through the config: `process.command`,
`config_snapshots`, detectors (`pattern`, `traceback`, `stall`,
`exit_code`), and rules. `healing.claude.subagents` controls which
subagent type gets spawned in phase 5 — defaults to `general-purpose`
but the user can route per-detector (e.g. `training_stall` →
specialized agent).

## Phase 3 — launch

```bash
# Foreground (first run; you see everything)
autosentry run

# Background (long-running supervision)
nohup autosentry run > /dev/null 2>&1 &
tail -F .autosentry/logs/autosentry.log
```

## Phase 4 — operate

```bash
autosentry status                    # snapshot of state.json
autosentry watch                     # live TUI
autosentry web                       # browse incidents in a browser
autosentry incidents list/show       # browse incident folders
autosentry analyze --since 24h       # ledger summary
autosentry update --check            # is there a newer release?
```

If three+ Claude-fixed incidents for the same detector all `kept`,
propose a YAML rule that codifies the pattern.

## Bounds

- **In phase 5, ALWAYS spawn a subagent.** Don't diagnose in-session.
  The user is operating other things; the subagent is your isolated
  diagnosis context.
- Never modify `.autosentry/state.json` or `.autosentry/attempts.tsv`.
- Never merge `autosentry/fix-*` branches that didn't verify.
- When editing the user's repo, leave a small, reviewable diff.
- Don't disable detectors to silence them. Tighten thresholds.

## Style

Be terse. One or two sentences per action. The structured log, the
incident folders, and the attempts ledger are the user's actual
interface — point at them; don't re-narrate them.
