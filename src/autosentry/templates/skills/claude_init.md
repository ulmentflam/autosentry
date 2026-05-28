---
description: Set up autosentry in this repo — install, scaffold the .autosentry/ tree, propose detectors and rules for the current stack.
argument-hint: ""
allowed-tools: Bash, Read, Edit, Write
---

# /autosentry-init — Claude Code

Onboard the current repo to autosentry. Stop and confirm before any
destructive action.

## Pre-flight

```bash
command -v autosentry && autosentry --version
{ [ -f .autosentry/autosentry.yaml ] || [ -f autosentry.yaml ]; } && echo configured || echo fresh-repo
```

## Steps

1. **Not installed?** Suggest:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh
   ```
   Confirm with the user before running. Verify with `autosentry --version`.

2. **No .autosentry/autosentry.yaml?** Run `autosentry init --non-interactive`.
   **Already exists?** Run `autosentry init --upgrade` and ask before
   accepting each diff (or pair with `--force` if the user OKs it
   wholesale).

3. **Inspect the repo** to propose a `process.command`. Look at
   `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `Makefile`,
   `scripts/`. **Ask the user** before editing `.autosentry/autosentry.yaml`.

4. **Propose `config_snapshots`** — `.env`, `configs/*.yaml`,
   `pyproject.toml`, anything that's effectively the "run config."
   Only list files that exist.

5. **Propose detectors/rules** tailored to what you saw:
   - ML/Python: oom, nccl, traceback, stall with `step (\\d+)` regex.
   - Service: 5xx patterns, connection-refused, traceback.
   - ETL: stage markers, no-output stall.
   Keep rules conservative; let Claude handle the rest.

6. **Install AGENTS.md** so future sessions pick up the full playbook:
   ```bash
   autosentry skills install --tool agents
   ```
   Add `--scope global` if the user wants it across every repo.

7. **Run `autosentry doctor`.** Fix any red row before moving on.

8. **Hand off the start command** but don't run it:
   ```bash
   nohup autosentry run > /dev/null 2>&1 &
   tail -F .autosentry/logs/autosentry.log
   ```

One-line responses per step. The user drives.
