---
description: Bootstrap, configure, or operate autosentry — a self-healing supervisor for long-running processes.
agent: build
---

# /autosentry — OpenCode

Full playbook lives in [`AGENTS.md`](../../AGENTS.md). Quick router:

1. `command -v autosentry` → missing → suggest the install one-liner.
2. `[ -f .autosentry/autosentry.yaml ] || [ -f autosentry.yaml ]` → missing → `autosentry init`, then help fill
   in `process.command`, `config_snapshots`, detectors, and rules
   matched to the user's stack.
3. `[ -f .autosentry/state.json ]` → missing → launch:
   `nohup autosentry run > /dev/null 2>&1 &` and tail
   `.autosentry/logs/autosentry.log`.
4. Running → use `autosentry status`, `autosentry watch`,
   `autosentry incidents list/show`, `autosentry analyze`.
5. `autosentry update --check` (cached daily) → if it prints
   `→ update available`, mention it once and recommend the command it
   shows (`autosentry update`, or `brew upgrade autosentry` for
   Homebrew). Don't upgrade unprompted.

If three+ Claude-fixed incidents for the same detector all `kept`,
propose a YAML rule that codifies the pattern. If a detector has a
regression streak ≥ 2 in `attempts.tsv`, diagnose by reading the
matching `autosentry/fix-<id>` branch.

One or two sentences per action.
