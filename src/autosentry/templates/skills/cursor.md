# /autosentry — Cursor

Routing prompt for Cursor's `/autosentry` slash command. The full
playbook is in `AGENTS.md` at the repo root — load that first.

## Phase detection

```bash
command -v autosentry && autosentry --version       # installed?
[ -f autosentry.yaml ]                              # configured?
[ -f .autosentry/state.json ]                       # running?
autosentry update --check                           # newer release? (cached daily)
```

If `autosentry update --check` prints `→ update available`, mention it
once and recommend the command it shows (`autosentry update`, or
`brew upgrade autosentry` for Homebrew). Don't upgrade unprompted.

## Routes

- **Not installed.** Suggest:
  `curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh`
- **Installed but no config.** Run `autosentry init`. Help fill in
  `process.command`, `config_snapshots`, and a starting
  `detectors:` / `rules:` set matched to the user's stack.
- **Config but no state.** Start it:
  `nohup autosentry run > /dev/null 2>&1 &` and
  `tail -F .autosentry/logs/autosentry.log`.
- **Running.** Use `autosentry status`, `autosentry watch` (TUI),
  `autosentry web` (browser), `autosentry incidents list/show`,
  `autosentry analyze --since 24h`.

## Style

Be terse. Point at the structured log, incident folders, and
`attempts.tsv` — they're the user's real interface. Don't re-narrate
them.
