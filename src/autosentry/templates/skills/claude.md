---
description: Bootstrap, configure, or operate autosentry — a self-healing supervisor for long-running processes.
argument-hint: "[install|init|run|watch|web|analyze|incidents|update]"
allowed-tools: Bash, Read, Edit, Write
---

# /autosentry — Claude Code

Follow the full playbook in [`AGENTS.md`](../../AGENTS.md). Quick triage:

```bash
command -v autosentry && autosentry --version
{ [ -f .autosentry/autosentry.yaml ] || [ -f autosentry.yaml ]; } && echo configured
[ -f .autosentry/state.json ] && jq -r '{pid, restarts, last_heartbeat}' .autosentry/state.json
autosentry update --check   # newer release? (cached daily)
```

If `autosentry update --check` prints `→ update available`, tell the user
once and recommend the command it shows (`autosentry update`, or
`brew upgrade autosentry` for Homebrew). Don't upgrade unprompted.

Common entry points:

- Install: `curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh`
- Initialize: `autosentry init`
- Start in background: `nohup autosentry run > /dev/null 2>&1 &`
- Live TUI: `autosentry watch`
- Web viewer: `autosentry web`
- Ledger summary: `autosentry analyze --since 24h`
- Check for updates: `autosentry update --check` (then `autosentry update`)
- Last incident: `autosentry incidents show "$(ls -1t .autosentry/incidents | head -n1)"`

If `$ARGUMENTS` was passed, route to the matching subcommand. Otherwise
detect the phase from the checks above and offer the next step.

Be terse. Point at the structured log, incident folders, and
`attempts.tsv` — don't re-narrate them.
