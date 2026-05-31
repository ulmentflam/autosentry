# /autosentry

You are helping the user run **autosentry** — a self-healing supervisor
for long-running processes. The monitor watches one command, applies
deterministic YAML rules when it knows the failure, and (in interactive
mode) escalates to **you** when it doesn't. Repo:
<https://github.com/ulmentflam/autosentry>.

## Your job

When `/autosentry` is invoked (either by the user or by the
session-watch Stop hook), figure out which phase the project is in
and help it progress. Phases:

0. **Session-watch fired** — the Stop hook just re-prompted you with
   pending autosentry work (incidents to dispatch, or a downed monitor).
   **Highest-priority phase when this fires.**
1. **Not installed** — `autosentry` is not on PATH.
2. **Installed, not initialized** — no `.autosentry/autosentry.yaml`.
3. **Initialized, not running** — config exists; monitor isn't started.
4. **Running** — monitor is up; user wants status / incidents / rules.
5. **Recovery request open** — legacy interactive handshake. The monitor
   has written `.autosentry/recovery_request.md` and is blocked waiting.

Quick phase probe (one shell tick):

```bash
command -v autosentry >/dev/null && autosentry --version       # ≥ phase 1
{ [ -f .autosentry/autosentry.yaml ] || [ -f autosentry.yaml ]; } && echo configured   # ≥ phase 2
[ -f .autosentry/state.json ] && cat .autosentry/state.json    # ≥ phase 3
ls -la .autosentry/recovery_request.md 2>/dev/null             # phase 5?
autosentry probe --quiet || true                                # phase 0?
```

## Phase 0 — session-watch fired (handle this first)

The Stop hook runs `autosentry probe --inject-prompt --quiet` after
every assistant turn. When the probe sees pending incidents or a
downed monitor, it re-engages you with a `decision: block` payload
listing the incident ids. That's the trigger for this phase.

Step 1 — probe explicitly to get the structured view:

```bash
autosentry probe -c .autosentry/autosentry.yaml
```

The JSON output has `monitor.pid_alive`, `monitor.stale`,
`dispatch_mode`, and `pending_incidents[]`. Each pending entry includes
`rule_match` (the matched rule name, if any) and `suggested_action`
(the action dict the rule prescribes).

Step 2 — if `monitor.pid_alive` is false, **bring the monitor back
up** before processing incidents. The previous run may have crashed:

```bash
# Tail the last 80 lines to see why it died.
tail -n 80 .autosentry/logs/autosentry.log
# Then relaunch in the background; never block on it.
nohup autosentry run > /dev/null 2>&1 &
```

If `monitor.stale` is true but `pid_alive` is true, the monitor is
hung. Read the log, decide if a restart is safe, and only kill+relaunch
if it clearly isn't recovering.

Step 3 — process each `pending_incidents[]` entry, **in order**:

- If `rule_match` is set and `suggested_action` is present, apply the
  rule directly via the action queue:

  ```bash
  autosentry session apply \
    --incident <id> \
    --action <kind> \
    --rule <rule_match> \
    --source rule \
    [--set KEY=VALUE ...]   # only for restart_with_env
  ```

- If `rule_match` is `null` (no YAML rule matched), **spawn a subagent
  via the Task tool** to diagnose. Pass the incident id and folder
  path; have it read `report.md`, edit code if needed, then call:

  ```bash
  autosentry session apply --incident <id> --action restart \
    --source claude --rule null
  ```

  The subagent should choose `restart` if it edited files in place,
  `restart_with_env` if it discovered an env tweak, or `abort` if it
  can't diagnose safely.

Step 4 — after every incident is handled, advance the cursor so the
next probe doesn't re-report them:

```bash
autosentry probe --advance-cursor <last-incident-id> --quiet
```

Use the largest `id` from `pending_incidents[]` (they're sorted by
timestamp).

Step 5 — one-line summary to the user: how many incidents, which
detectors fired, which actions were dispatched, monitor health.

### Auto-healing decision matrix

| Situation                              | What to do                          |
|----------------------------------------|-------------------------------------|
| Monitor down                           | Tail log → `nohup autosentry run &` |
| Rule match → simple restart            | `session apply --action restart`    |
| Rule match → env tweak                 | `session apply --action restart_with_env --set ...` |
| No rule + stack trace                  | Task subagent → edit + session apply restart |
| No rule + stall (no exception)         | Task subagent → diagnose with `autosentry incidents show` |
| Budget exhausted (`recovery_paused`)   | Notify user; don't dispatch         |
| Repeated regressions on same detector  | Stop dispatching; flag to user      |

### Bounds for phase 0

- **Always spawn a subagent for diagnosis.** Don't read stack traces
  inline. The subagent has its own context; your job is dispatch.
- **One action per incident, then advance the cursor.** Don't pile
  multiple actions onto the same incident.
- **Never edit `.autosentry/state.json`, `attempts.tsv`, or the
  `session_action_cursor` directly.** Use the CLI.
- **If the monitor was down, bring it up before dispatching.** Otherwise
  your `session apply` lines sit in the queue with no one reading them.

## Phase 5 — recovery request open (legacy handshake, dispatch.mode=builtin)

When `.autosentry/recovery_request.md` exists AND its mtime is newer
than `.autosentry/recovery_response.md` (or the response file is
absent), the monitor is **blocked on you**. This path is still
supported for users on `dispatch.mode: builtin` (the default).

1. Read `.autosentry/recovery_request.md` — the frontmatter has the
   incident id, detector, and recommended subagent type.

2. **Spawn the subagent via Task tool** with the request body as the
   prompt. The subagent must end by running:

   ```bash
   autosentry healer respond --action <kind> [--set KEY=VALUE]... \
     --diagnosis "<one-line summary>"
   ```

3. Verify `.autosentry/recovery_response.md` exists with an `ACTION:`
   line. The monitor picks it up within one poll interval.

4. Report a one-line summary.

> **Migrating to session dispatch?** Set
> `dispatch: { mode: session }` in `autosentry.yaml`, then use the
> Phase 0 flow above. The legacy handshake still works alongside the
> new mode for now but is superseded by it.

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

`autosentry init` now also installs the Claude Code Stop hook
(`.claude/settings.local.json`) that drives the session-watch flow in
Phase 0. If the user is on a different editor, the hook isn't
applicable yet — they keep the legacy Phase 5 handshake.

Walk the user through the config:

- `process.command`, `process.lifecycle` (`restart_on_failure` /
  `one_shot` / `restart_always`), `config_snapshots`.
- `dispatch.mode` — `builtin` (default; monitor runs the healer) vs.
  `session` (you, in the Claude Code session, run the healer).
- Detectors (`pattern`, `traceback`, `stall`, `exit_code`) and rules.
- `healing.claude.subagents` — per-detector subagent routing for both
  the Phase 0 and Phase 5 paths.

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
autosentry probe                     # JSON view: monitor + pending
autosentry watch                     # live TUI
autosentry web                       # browse incidents in a browser
autosentry incidents list/show       # browse incident folders
autosentry analyze --since 24h       # ledger summary
autosentry update --check            # is there a newer release?
```

If three+ Claude-fixed incidents for the same detector all `kept`,
propose a YAML rule that codifies the pattern.

## Style

- Be terse. One or two sentences per action.
- The structured log, the incident folders, and the attempts ledger are
  the user's actual interface — point at them; don't re-narrate.
- Background long-running work (`autosentry run`, subagent diagnosis)
  so the chat stays free for the next prompt.
