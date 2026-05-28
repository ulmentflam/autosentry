# /autosentry — Aider conventions

This file lives at `.aider/CONVENTIONS.md` and is auto-discovered by
Aider. Treat it as a short complement to `AGENTS.md` (the full
playbook), focused on Aider-specific guidance.

## When the user asks about autosentry

Read `AGENTS.md` first — Aider has it on as a `read:` file via
`.aider.conf.yml`. Then route by phase:

```bash
command -v autosentry && autosentry --version       # installed?
[ -f .autosentry/autosentry.yaml ] || [ -f autosentry.yaml ]   # configured?
[ -f .autosentry/state.json ]                       # running?
```

## Editing rules

When you're proposing an Aider edit to fix a recurring issue
autosentry has flagged:

- The diff should be small and reviewable. autosentry captures every
  edit into the incident folder; honor that workflow.
- Don't disable detectors to silence them. Tighten thresholds
  instead.
- If three+ Claude-fixed incidents for the same detector all `kept`,
  propose a YAML rule update in `.autosentry/autosentry.yaml` rather than a code
  change.

## Style

Aider's chat output goes to the user. Be terse; one or two sentences
per action.
