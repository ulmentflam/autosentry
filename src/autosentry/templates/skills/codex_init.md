# /autosentry-init — Codex CLI

Onboard a fresh repo to autosentry. Stop and confirm before any
destructive action.

```bash
command -v autosentry && autosentry --version
[ -f autosentry.yaml ] && echo configured || echo fresh
```

Flow:

1. Missing CLI → suggest:
   `curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh`
2. Missing config → `autosentry init --non-interactive`. Present
   config → `autosentry init --upgrade`.
3. Read the repo, propose `process.command`. **Ask the user** before
   editing autosentry.yaml.
4. Propose `config_snapshots` from `.env`, `configs/*.yaml`,
   `pyproject.toml` (only existing files).
5. Propose starter detectors + rules for the observed stack.
6. `autosentry skills install --tool agents` (add `--scope global` if
   the user wants every repo to inherit).
7. `autosentry doctor` — fix red rows; summarize warnings.
8. Hand off the `nohup autosentry run …` command; don't run it.

One sentence per step.
