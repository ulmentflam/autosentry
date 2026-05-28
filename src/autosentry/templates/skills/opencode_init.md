---
description: Set up autosentry in this repo — install, scaffold the .autosentry/ tree, propose detectors and rules for the current stack.
agent: build
---

# /autosentry-init — OpenCode

Onboard the current repo to autosentry. The condensed flow:

1. `command -v autosentry` → if missing, suggest the install one-liner
   (ask before running).
2. `[ -f .autosentry/autosentry.yaml ] || [ -f autosentry.yaml ]` → if missing, `autosentry init --non-interactive`;
   if present, `autosentry init --upgrade`.
3. Read the repo (pyproject.toml / package.json / Cargo.toml / go.mod
   / Makefile) and propose `process.command`. **Ask before editing.**
4. Propose `config_snapshots` (only files that exist).
5. Propose detectors/rules for the observed stack. Conservative
   restart-style rules; Claude handles novel failures.
6. `autosentry skills install --tool agents` so AGENTS.md lands.
7. `autosentry doctor` — fix red rows.
8. Hand the user the `nohup autosentry run …` command but don't
   execute it yourself.

One sentence per step. Don't narrate the docs.
