# Phase 5.6 — global skill scope + focused init skill

Two adjacent changes:

1. **Skill scope.** `autosentry skills install` currently only writes
   into the current repo. Some users want `/autosentry` available in
   *every* repo without re-running `skills install`. A new
   ``--scope global`` writes into the tool's home-directory location
   so every interactive session in every repo picks it up.

2. **Focused init skill.** The existing `/autosentry` skill is the
   full playbook (install → init → run → operate → recovery). A
   smaller `/autosentry-init` skill is useful when you just want
   "set this repo up" without the operator/recovery content. Lower
   reading cost for AI agents that have one job.

## CLI shape

```bash
# default (unchanged): local, full skill
autosentry skills install

# install just the init skill into the current repo
autosentry skills install --skill init

# install /autosentry globally for every tool that has a home-dir slot
autosentry skills install --scope global

# install both skills, globally, for one tool
autosentry skills install --tool claude --skill all --scope global
```

`--skill` accepts: `autosentry`, `init`, or `all` (default
`autosentry`).
`--scope` accepts: `local` (default), `global`.

## Per-tool global paths

| tool       | global path                              | notes |
|------------|------------------------------------------|-------|
| claude     | `~/.claude/commands/<name>.md`           | per Claude Code docs |
| opencode   | `~/.config/opencode/command/<name>.md`   | XDG-style |
| codex      | `~/.codex/prompts/<name>.md`             | per Codex CLI docs |
| gemini     | `~/.gemini/commands/<name>.toml`         | per Gemini CLI docs |
| cursor     | `~/.cursor/commands/<name>.md`           | not widely standardized; best guess |
| aider      | `~/.aider.conf.yml`                      | already global by convention |
| continue   | `~/.continue/config.json`                | already global by default |
| windsurf   | `~/.windsurfrules` (home-dir alias)      | falls back to per-repo on conflict |
| zed        | `~/.config/zed/prompts/<name>.md`        | per Zed docs |
| agents     | (no global)                              | AGENTS.md is repo-specific by design |

`agents` returns an error when installed with `--scope global` — the
AGENTS.md convention is per-repo only.

## Data shape change

```python
@dataclass(frozen=True)
class SkillTarget:
    tool: ToolName
    skill: SkillName              # NEW — "autosentry" | "init"
    template: str
    destination: Path             # local path (relative to target_dir)
    global_destination: Path | None  # NEW — absolute, expanded; None = no global
    label: str
```

`install(target_dir, *, tools, skill_name="autosentry", scope="local", force=False)` —
existing callers default to the same behavior.

## Task ledger

| #  | task                                             | status |
|----|--------------------------------------------------|--------|
| 87 | This plan                                        | wip    |
| 88 | SkillTarget refactor + scope handling            | todo   |
| 89 | Init skill templates                             | todo   |
| 90 | CLI --skill and --scope flags                    | todo   |
| 91 | Tests + 0.7.0 bump                               | todo   |

## Out of scope

- Auto-syncing globally installed skills when the autosentry version
  updates (users re-run `autosentry skills install --scope global` by
  hand; punt the dispatcher-style auto-refresh for later).
- Per-host customization of slash command names (everyone gets
  `/autosentry` and `/autosentry-init`; future Phase 6 candidate).
