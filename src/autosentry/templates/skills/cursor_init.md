# /autosentry-init — Cursor

Focused onboarding for the current repo. Stop and confirm before any
destructive action.

```bash
command -v autosentry && autosentry --version       # installed?
[ -f .autosentry/autosentry.yaml ] || [ -f autosentry.yaml ]   # configured?
```

Steps:

1. **Not installed** → suggest the one-liner, ask before running:
   `curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh`
2. **No config** → `autosentry init --non-interactive`.
   **Config exists** → `autosentry init --upgrade`.
3. Inspect the repo; propose `process.command`. **Ask user first.**
4. Propose `config_snapshots` (existing files only).
5. Propose detectors and rules for the observed stack.
6. `autosentry skills install --tool agents` (use `--scope global`
   if the user wants every repo to inherit /autosentry).
7. `autosentry doctor` — fix reds.
8. Hand off `nohup autosentry run …`; don't run it.

One sentence per step.
