# Windsurf Cascade — autosentry rules

This file lives at `.windsurfrules` at the repo root and is loaded by
Windsurf's Cascade agent on every conversation in this workspace.

## When the user mentions autosentry

Open `AGENTS.md` at the repo root and follow that playbook. The
condensed router:

1. `command -v autosentry && autosentry --version` → if missing,
   suggest:
   `curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh`
2. `[ -f .autosentry/autosentry.yaml ] || [ -f autosentry.yaml ]` → if missing, `autosentry init` and walk
   the user through `process.command`, `config_snapshots`, detectors,
   and rules.
3. `[ -f .autosentry/state.json ]` → if missing, launch:
   `nohup autosentry run > /dev/null 2>&1 &`
4. Running → `autosentry status`, `autosentry watch`,
   `autosentry web`, `autosentry incidents list/show`,
   `autosentry analyze --since 24h`.

## Key facts

- **Fix branches.** Claude-driven fixes land on
  `autosentry/fix-<incident-id>` and only stick if the same detector
  doesn't re-fire inside `healing.verify_window_seconds` (default
  600s). Don't merge a fix branch that hasn't verified.
- **`attempts.tsv` is the ledger.** Trust its `status` column:
  `kept` / `regressed` / `crashed`.
- **High-leverage move:** when three+ Claude fixes for the same
  detector all `kept`, propose a YAML rule that codifies the
  pattern. Every codified rule turns a slow Claude call into a
  deterministic restart.

## Bounds

- Don't modify `.autosentry/state.json` or `.autosentry/attempts.tsv`
  directly.
- Don't disable detectors to silence them — tighten thresholds.
- Keep edits small and reviewable; autosentry captures diffs into
  the incident folder.

## Style

Be terse. One or two sentences per action.
