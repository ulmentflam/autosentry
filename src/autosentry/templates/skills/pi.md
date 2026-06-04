---
description: Bootstrap, configure, or operate autosentry — a self-healing supervisor for long-running processes.
argument-hint: "[install|init|run|watch|web|analyze|incidents|update]"
---

# /autosentry — Pi

Routing prompt for Pi's `/autosentry` slash command. The full playbook
is in `AGENTS.md` at the repo root — load that first.

## Phase detection

```bash
command -v autosentry && autosentry --version
{ [ -f .autosentry/autosentry.yaml ] || [ -f autosentry.yaml ]; } && echo configured
[ -f .autosentry/state.json ] && cat .autosentry/state.json
autosentry update --check   # newer release? (cached daily)
```

If `autosentry update --check` prints `→ update available`, mention it
once and recommend the command it prints (`autosentry update`, or
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
