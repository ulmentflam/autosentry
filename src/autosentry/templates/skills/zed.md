# /autosentry — Zed prompt

This file lives at `.zed/prompts/autosentry.md` and is invoked in Zed
as the slash command `/autosentry`.

You are helping the user run **autosentry** — a self-healing
supervisor for long-running processes. Open `AGENTS.md` at the repo
root for the complete playbook. Quick router:

1. `command -v autosentry && autosentry --version` → not installed?
   Suggest:
   `curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh`
2. `[ -f .autosentry/autosentry.yaml ] || [ -f autosentry.yaml ]` → not configured? Run `autosentry init`
   and configure `process.command`, `config_snapshots`, detectors,
   and rules.
3. `[ -f .autosentry/state.json ]` → not running? Launch:
   `nohup autosentry run > /dev/null 2>&1 &` and
   `tail -F .autosentry/logs/autosentry.log`.
4. Running? Use `autosentry status`, `autosentry watch`,
   `autosentry web`, `autosentry incidents list/show`,
   `autosentry analyze --since 24h`.
5. `autosentry update --check` (cached daily) → if it prints
   `→ update available`, mention it once and recommend the command it
   shows (`autosentry update`, or `brew upgrade autosentry` for
   Homebrew). Don't upgrade unprompted.

Claude-driven fixes land on `autosentry/fix-<incident-id>` branches
and only stick if the same detector doesn't re-fire inside the
verify window. `attempts.tsv` is the audit trail.

Be terse. One or two sentences per action.
