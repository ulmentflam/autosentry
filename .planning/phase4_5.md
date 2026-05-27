# Phase 4.5 — skill refresh + new AI-tool wrappers

The Phase 2 skills shipped before Phase 3 and 4 features landed. They're
out of date and don't mention `autosentry watch`, `autosentry web`,
`autosentry analyze`, fix branches, the attempts ledger, the Slack
inbox commands, or the operator `program.md`. We also lack wrappers
for four widely-used AI editors that have asked for them: Aider,
Continue.dev, Windsurf, Zed.

## What changes

1. **`AGENTS.md` becomes the single authoritative playbook.** It already
   was, but right now it's stale. We refresh it to cover every feature
   shipped through Phase 4 and trim per-tool wrappers down to thin
   shims that defer to it. Lower duplication, easier to keep current.
2. **The canonical `autosentry.md` skill** gets the same refresh so
   the prompt template embedded in non-AGENTS-aware tools matches.
3. **Four new tools** get first-class wrappers:
   - **Aider** — `.aider.conf.yml` configured to `read: AGENTS.md`.
     Aider treats the file as ambient context for every session.
   - **Continue.dev** — `.continue/config.json` with a custom prompt
     entry that loads AGENTS.md content (or, when loading at runtime
     isn't supported, embeds the canonical playbook inline).
   - **Windsurf** — `.windsurfrules` at the repo root. Plain markdown.
     Cascade reads it on every conversation.
   - **Zed** — `.zed/prompts/autosentry.md`. Zed's slash-command
     convention; invoked as `/autosentry`.

## Task ledger

| #  | task                                                | status |
|----|-----------------------------------------------------|--------|
| 46 | This plan                                           | done   |
| 47 | Refresh AGENTS.md                                   | done   |
| 48 | Refresh canonical autosentry.md                     | done   |
| 49 | Slim per-tool wrappers (claude/codex/cursor/gemini/opencode) | done |
| 50 | Add Aider / Continue / Windsurf / Zed wrappers      | done   |
| 51 | Bump 0.4.1 + CHANGELOG + README                     | done (rolled into 0.5.0 with Discord) |

## Tool conventions (locked-in choices)

| tool      | path the wrapper lands at                     | format |
|-----------|-----------------------------------------------|--------|
| Aider     | `.aider.conf.yml` + `.aider/CONVENTIONS.md`   | yaml + md |
| Continue  | `.continue/config.json` (merged if exists)    | json  |
| Windsurf  | `.windsurfrules` at repo root                 | md    |
| Zed       | `.zed/prompts/autosentry.md`                  | md    |

Conventions confirmed (May 2026):

- Aider supports `--read FILE` and `.aider.conf.yml`'s `read:` key
  to inject ambient context into every chat. Listing `AGENTS.md`
  there is the canonical way to bind it.
- Continue.dev's `config.json` exposes a `prompts:` array (or, in
  newer versions, a `customCommands` array) — we ship a prompt
  entry that quotes the canonical playbook.
- Windsurf reads `.windsurfrules` (markdown) at repo root for the
  Cascade agent. Newer "Windsurf workflows" use `.windsurf/rules/`
  but `.windsurfrules` is still honored as a fallback.
- Zed's prompts live under `.zed/prompts/` (project) or
  `~/.config/zed/prompts/` (global). One file per slash command.

## Out of scope

- Cline (`.clinerules`) and Roo Code (`.roo/`) — can land later if
  there's demand.
- Updating the README's full tool matrix table to include a
  feature-by-feature compatibility breakdown. The CLI's `skills list`
  already enumerates the destinations.
