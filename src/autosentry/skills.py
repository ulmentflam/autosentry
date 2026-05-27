"""Skill installer — drops per-tool slash-command wrappers into a user repo
(``--scope local``, default) OR into the user's home for every interactive
session (``--scope global``).

Each supported AI tool reads its slash-command definitions from a specific
path under its own convention. This module owns the mapping from
``(tool, skill, scope)`` → template source → destination path, and the
copy logic.

Templates ship inside the package at ``src/autosentry/templates/skills/``
so an installed wheel has everything it needs without going back to the
network.

Two skills land here:

- ``autosentry`` — the full /autosentry slash command playbook (install
  → init → run → operate → interactive recovery).
- ``init`` — a focused /autosentry-init slash command that ONLY does the
  per-repo setup flow. Smaller reading cost for AI agents when you just
  want a fresh repo onboarded.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ToolName = Literal[
    "claude",
    "opencode",
    "codex",
    "gemini",
    "cursor",
    "aider",
    "continue",
    "windsurf",
    "zed",
    "agents",
]

ALL_TOOLS: tuple[ToolName, ...] = (
    "claude",
    "opencode",
    "codex",
    "gemini",
    "cursor",
    "aider",
    "continue",
    "windsurf",
    "zed",
    "agents",
)

SkillName = Literal["autosentry", "init"]
ALL_SKILLS: tuple[SkillName, ...] = ("autosentry", "init")

Scope = Literal["local", "global"]
ALL_SCOPES: tuple[Scope, ...] = ("local", "global")


@dataclass(frozen=True)
class SkillTarget:
    """One (tool, skill) binding with paths for both scopes."""

    tool: ToolName
    skill: SkillName
    template: str  # filename under templates/skills/
    local_destination: Path  # relative to the target repo root
    global_destination: Path | None  # absolute (home-anchored); None = no global
    label: str  # human description for status output

    @property
    def destination(self) -> Path:
        """Back-compat alias — early-Phase code referenced ``destination``."""
        return self.local_destination


def _home() -> Path:
    return Path.home()


# Local paths are relative; global paths anchor on ~/. ``agents`` has no
# global slot — AGENTS.md is repo-specific by design.
_TARGETS: dict[tuple[ToolName, SkillName], SkillTarget] = {
    # ----- autosentry (full playbook) -----
    ("claude", "autosentry"): SkillTarget(
        tool="claude",
        skill="autosentry",
        template="claude.md",
        local_destination=Path(".claude/commands/autosentry.md"),
        global_destination=_home() / ".claude/commands/autosentry.md",
        label="Claude Code slash command",
    ),
    ("opencode", "autosentry"): SkillTarget(
        tool="opencode",
        skill="autosentry",
        template="opencode.md",
        local_destination=Path(".opencode/command/autosentry.md"),
        global_destination=_home() / ".config/opencode/command/autosentry.md",
        label="OpenCode slash command",
    ),
    ("codex", "autosentry"): SkillTarget(
        tool="codex",
        skill="autosentry",
        template="codex.md",
        local_destination=Path(".codex/prompts/autosentry.md"),
        global_destination=_home() / ".codex/prompts/autosentry.md",
        label="OpenAI Codex CLI prompt",
    ),
    ("gemini", "autosentry"): SkillTarget(
        tool="gemini",
        skill="autosentry",
        template="gemini.toml",
        local_destination=Path(".gemini/commands/autosentry.toml"),
        global_destination=_home() / ".gemini/commands/autosentry.toml",
        label="Gemini (Antigravity / CLI) command",
    ),
    ("cursor", "autosentry"): SkillTarget(
        tool="cursor",
        skill="autosentry",
        template="cursor.md",
        local_destination=Path(".cursor/commands/autosentry.md"),
        global_destination=_home() / ".cursor/commands/autosentry.md",
        label="Cursor slash command",
    ),
    ("aider", "autosentry"): SkillTarget(
        tool="aider",
        skill="autosentry",
        template="aider.conf.yml",
        local_destination=Path(".aider.conf.yml"),
        global_destination=_home() / ".aider.conf.yml",
        label="Aider config — adds AGENTS.md to every chat as read context",
    ),
    ("continue", "autosentry"): SkillTarget(
        tool="continue",
        skill="autosentry",
        template="continue.config.json",
        local_destination=Path(".continue/config.json"),
        global_destination=_home() / ".continue/config.json",
        label="Continue.dev custom command (`/autosentry`)",
    ),
    ("windsurf", "autosentry"): SkillTarget(
        tool="windsurf",
        skill="autosentry",
        template="windsurfrules.md",
        local_destination=Path(".windsurfrules"),
        global_destination=_home() / ".windsurfrules",
        label="Windsurf Cascade rules at repo root",
    ),
    ("zed", "autosentry"): SkillTarget(
        tool="zed",
        skill="autosentry",
        template="zed.md",
        local_destination=Path(".zed/prompts/autosentry.md"),
        global_destination=_home() / ".config/zed/prompts/autosentry.md",
        label="Zed slash command (`/autosentry`)",
    ),
    ("agents", "autosentry"): SkillTarget(
        tool="agents",
        skill="autosentry",
        template="AGENTS.md",
        local_destination=Path("AGENTS.md"),
        global_destination=None,  # AGENTS.md is repo-specific
        label="Universal AGENTS.md (read by Codex, Gemini, Cursor, OpenCode, Aider)",
    ),
    # ----- init (focused setup) -----
    ("claude", "init"): SkillTarget(
        tool="claude",
        skill="init",
        template="claude_init.md",
        local_destination=Path(".claude/commands/autosentry-init.md"),
        global_destination=_home() / ".claude/commands/autosentry-init.md",
        label="Claude Code `/autosentry-init` slash command",
    ),
    ("opencode", "init"): SkillTarget(
        tool="opencode",
        skill="init",
        template="opencode_init.md",
        local_destination=Path(".opencode/command/autosentry-init.md"),
        global_destination=_home() / ".config/opencode/command/autosentry-init.md",
        label="OpenCode `/autosentry-init` slash command",
    ),
    ("codex", "init"): SkillTarget(
        tool="codex",
        skill="init",
        template="codex_init.md",
        local_destination=Path(".codex/prompts/autosentry-init.md"),
        global_destination=_home() / ".codex/prompts/autosentry-init.md",
        label="Codex CLI `/autosentry-init` prompt",
    ),
    ("gemini", "init"): SkillTarget(
        tool="gemini",
        skill="init",
        template="gemini_init.toml",
        local_destination=Path(".gemini/commands/autosentry-init.toml"),
        global_destination=_home() / ".gemini/commands/autosentry-init.toml",
        label="Gemini `/autosentry-init` command",
    ),
    ("cursor", "init"): SkillTarget(
        tool="cursor",
        skill="init",
        template="cursor_init.md",
        local_destination=Path(".cursor/commands/autosentry-init.md"),
        global_destination=_home() / ".cursor/commands/autosentry-init.md",
        label="Cursor `/autosentry-init` slash command",
    ),
    ("zed", "init"): SkillTarget(
        tool="zed",
        skill="init",
        template="zed_init.md",
        local_destination=Path(".zed/prompts/autosentry-init.md"),
        global_destination=_home() / ".config/zed/prompts/autosentry-init.md",
        label="Zed `/autosentry-init` prompt",
    ),
    # Aider, Continue, Windsurf, and agents don't have per-skill slash
    # commands — they're ambient-context or universal files. The full
    # /autosentry wrappers for those tools already mention init, so we
    # don't ship duplicate init wrappers for them.
}


@dataclass
class InstallResult:
    target: SkillTarget
    written: Path
    status: Literal["created", "skipped_exists", "overwritten", "skipped_no_global"]


def templates_dir() -> Path:
    """Resolve the bundled skills/ directory inside the installed package."""
    return Path(__file__).parent / "templates" / "skills"


def targets_for(
    tools: Iterable[ToolName] | None,
    *,
    skill_name: SkillName | Literal["all"] = "autosentry",
) -> list[SkillTarget]:
    """Return the SkillTarget set matching ``tools`` × ``skill_name``.

    ``tools=None`` → every tool that has a binding for the requested
    skill. ``skill_name="all"`` → every skill (autosentry + init) for
    each chosen tool.
    """
    skill_filter: tuple[SkillName, ...]
    if skill_name == "all":
        skill_filter = ALL_SKILLS
    else:
        skill_filter = (skill_name,)

    if tools:
        tool_filter = list(_unique_preserving_order(tools))
        for t in tool_filter:
            if t not in ALL_TOOLS:
                msg = f"unknown tool: {t} (choose from: {', '.join(ALL_TOOLS)})"
                raise ValueError(msg)
    else:
        tool_filter = list(ALL_TOOLS)

    out: list[SkillTarget] = []
    for tool in tool_filter:
        for skill in skill_filter:
            target = _TARGETS.get((tool, skill))
            if target is not None:
                out.append(target)
    return out


def install(
    target_dir: Path,
    *,
    tools: Iterable[ToolName] | None = None,
    skill_name: SkillName | Literal["all"] = "autosentry",
    scope: Scope = "local",
    force: bool = False,
) -> list[InstallResult]:
    """Drop the configured skill files into ``target_dir`` (or ``~/`` when
    ``scope='global'``).

    Returns one :class:`InstallResult` per attempted (tool, skill) pair.
    Targets that have no ``global_destination`` (currently just ``agents``)
    return status ``skipped_no_global`` when ``scope='global'`` — that's a
    soft skip, not an error.

    Raises :class:`FileNotFoundError` only on packaging bugs (missing
    template files).
    """
    src_dir = templates_dir()
    if not src_dir.is_dir():
        msg = f"bundled skill templates not found at {src_dir}"
        raise FileNotFoundError(msg)

    target_dir = target_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    results: list[InstallResult] = []
    for target in targets_for(tools, skill_name=skill_name):
        src = src_dir / target.template
        if not src.is_file():
            msg = f"missing bundled template: {src}"
            raise FileNotFoundError(msg)

        if scope == "global":
            dest = target.global_destination
            if dest is None:
                results.append(
                    InstallResult(
                        target=target,
                        written=target.local_destination,
                        status="skipped_no_global",
                    )
                )
                continue
        else:
            dest = target_dir / target.local_destination

        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not force:
            results.append(InstallResult(target=target, written=dest, status="skipped_exists"))
            continue
        status: Literal["created", "overwritten"] = "overwritten" if dest.exists() else "created"
        shutil.copy2(src, dest)
        results.append(InstallResult(target=target, written=dest, status=status))
    return results


# ----- helpers -------------------------------------------------------------


def _unique_preserving_order(items: Iterable[ToolName]) -> Iterable[ToolName]:
    seen: set[ToolName] = set()
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        yield x
