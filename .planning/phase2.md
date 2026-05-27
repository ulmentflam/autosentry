# Phase 2 — OSS housekeeping, distribution, and AI-tool skills

Tracks the in-flight work for Phase 2. Update statuses as items land.

## Goals

1. Open-source scaffolding (Apache 2.0, contributor docs, GitHub templates).
2. `install.sh` one-liner installable from a `curl | sh`.
3. `autosentry update` mechanism (CLI subcommand + standalone `update.sh`).
4. Slash-command / skill wrappers so users can launch autosentry from inside
   their interactive AI session in **Claude Code**, **OpenCode**, **OpenAI
   Codex CLI**, **Google Gemini (Antigravity)**, and **Cursor**.
5. A canonical `AGENTS.md` at the repo root so any agent that follows that
   emerging convention (Codex, Antigravity, Cursor, OpenCode) gets the same
   guidance without per-tool duplication.

## Task ledger

Statuses: `todo` · `wip` · `done` · `blocked`. Owner is the agent (you or me)
who picked the task up; leave blank when free.

| # | task                                                              | owner | status |
|---|-------------------------------------------------------------------|-------|--------|
| 15 | Apache 2.0 `LICENSE` + `NOTICE`; pyproject license metadata     | claude | done |
| 16 | `CONTRIBUTING.md` + `CHANGELOG.md` (Keep a Changelog format, seeded 0.1.0 + 0.2.0). Code of conduct, security policy, issue/PR templates, dependabot deferred to Phase 3. | claude | partial |
| 17 | Polished README (badges, ToC, feature matrix, install one-liner, incident anatomy, config reference, skills install, FAQ) | claude | done |
| 18 | `install.sh` (uv-preferred, pipx, pip fallback; pinned version via env; curl-from-GitHub one-liner) | claude | done |
| 19 | `autosentry update` CLI subcommand + `scripts/update.sh` (re-invokes same installer; `--check` reports current vs latest) | claude | done |
| 20 | Canonical `skills/autosentry.md` — the master prompt every per-tool skill includes | claude | done |
| 21 | Per-tool wrappers: `.claude/commands/autosentry.md`, `.opencode/command/autosentry.md`, `.codex/prompts/autosentry.md`, `.gemini/commands/autosentry.toml`, `.cursor/commands/autosentry.md`, root-level `AGENTS.md` | claude | done |
| 22 | New CLI `autosentry skills install [--tool {claude,opencode,codex,gemini,cursor,all}] [--force]` that drops the per-tool skill files into a user's repo | claude | done |
| 23 | Bump to 0.2.0, CHANGELOG entry | claude | done |

## Per-tool skill conventions (assumptions; correct as needed)

| tool                 | location                                         | format  | invoke |
|----------------------|--------------------------------------------------|---------|--------|
| Claude Code          | `.claude/commands/autosentry.md`                 | md+frontmatter | `/autosentry` |
| OpenCode             | `.opencode/command/autosentry.md`                | md+frontmatter | `/autosentry` |
| OpenAI Codex CLI     | `.codex/prompts/autosentry.md`                   | md      | `/autosentry` |
| Gemini Antigravity   | `.gemini/commands/autosentry.toml`               | toml (`prompt`, `description`) | `/autosentry` |
| Cursor               | `.cursor/commands/autosentry.md`                 | md      | `/autosentry` |
| Universal fallback   | `AGENTS.md` at repo root                         | md      | read-on-attach |

`autosentry skills install` ships templates for all of these and writes them
into the target repo. The canonical prompt lives in
`src/autosentry/templates/skills/autosentry.md` and is the body each per-tool
wrapper either embeds or references.

## What the skill should do

When a user types `/autosentry` inside their AI tool, the agent should:

1. Detect whether `autosentry.yaml` exists in the repo. If not, offer to
   bootstrap with `autosentry init` and walk them through filling in the
   `process.command` and `config_snapshots`.
2. Confirm relevant detectors and rules for the user's stack (Python? Node?
   Go? GPU training? Web server?).
3. Offer to launch `autosentry run` in the background and tail the structured
   log. Stop at the first incident and walk the user through the generated
   `.autosentry/incidents/<id>/report.md`.
4. For an existing setup: show recent incidents, surface trends, and propose
   new rules from anything that's recurred without a rule yet.

## Open questions

- Should `autosentry skills install` default to `--tool all` or prompt the
  user? Current plan: prompt unless `--tool` is given, with `all` as a
  documented one-liner.
- Do we ship a homebrew formula in Phase 2 or punt to Phase 3? Current plan:
  punt. `install.sh` covers macOS via `uv` or `pip`.
- Do we need a self-update mechanism for the skill files (so users can pull
  updated skill prompts when autosentry itself ships new ones)? Current
  plan: `autosentry skills install --force` re-deploys; users opt-in.

## Out of scope for Phase 2

- SLURM / Docker / Attach supervisor implementations (deferred to Phase 3).
- Status TUI / web viewer.
- Homebrew, apt, AUR, conda-forge packaging.
