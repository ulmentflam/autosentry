# /autosentry-init — Zed

Focused onboarding for the current repo. Stop and confirm before any
destructive action.

```bash
command -v autosentry && autosentry --version
{ [ -f .autosentry/autosentry.yaml ] || [ -f autosentry.yaml ]; } && echo configured || echo fresh
```

Steps:

1. Missing CLI → suggest:
   `curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh`
   Ask the user first.
2. No config → `autosentry init --non-interactive`. Existing →
   `autosentry init --upgrade`.
3. Read the repo, propose `process.command`. **Ask the user before
   editing .autosentry/autosentry.yaml.**
4. Propose `config_snapshots` (only files that exist).
5. Propose detectors + rules for the stack you saw.
6. `autosentry skills install --tool agents`. Add `--scope global` if
   the user wants every repo to inherit /autosentry.
7. `autosentry doctor` — fix reds.
8. Hand off `nohup autosentry run …`; don't run it.

One sentence per step. The full /autosentry skill has the operator
playbook for after the monitor is running.
