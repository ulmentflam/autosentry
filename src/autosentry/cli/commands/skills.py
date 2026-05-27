"""``autosentry skills`` subcommands — AI-editor wrapper installation."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import typer
from rich.table import Table

from autosentry import skills as skills_mod
from autosentry.cli import app
from autosentry.cli.style import ERR, OK, WARN, console

skills_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Install AI-tool slash-command wrappers "
        "(Claude / OpenCode / Codex / Gemini / Cursor / Aider / Continue / Windsurf / Zed). "
        "Two skills land here: `autosentry` (the full playbook) and `init` (focused "
        "setup). Use --scope global to install once and have every repo pick them up."
    ),
)
app.add_typer(skills_app, name="skills")


_VALID_SKILLS = ("autosentry", "init", "all")
_VALID_SCOPES = ("local", "global")


@skills_app.command("install")
def skills_install(
    tool: list[str] = typer.Option(  # noqa: B008
        [],
        "--tool",
        "-t",
        help=(
            "Which tool(s) to install for. Repeat or use 'all'. "
            "Choices: claude, opencode, codex, gemini, cursor, "
            "aider, continue, windsurf, zed, agents."
        ),
    ),
    skill: str = typer.Option(
        "autosentry",
        "--skill",
        "-s",
        help=(
            "Which skill to install. Choices: 'autosentry' (full playbook), "
            "'init' (focused setup), 'all'. Default: autosentry."
        ),
    ),
    scope: str = typer.Option(
        "local",
        "--scope",
        help=(
            "Where to install. 'local' (default) writes into the current repo. "
            "'global' writes into the user's home so every repo inherits the "
            "skill in every interactive session."
        ),
    ),
    target: Path = typer.Option(  # noqa: B008
        Path("."),
        "--target",
        help="Target repo directory for --scope local (default: cwd). Ignored when --scope global.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing files instead of skipping them."
    ),
) -> None:
    """Install AI-editor slash-command wrappers in a repo or in the user's home.

    Each --tool drops one file at its canonical location for the chosen
    --skill (default `autosentry`, the full playbook). The `init` skill
    is a smaller `/autosentry-init` slash command focused on per-repo
    setup. Pass `--skill all` to install both.

    `--scope global` writes to the tool's home-directory location instead
    of the current repo — handy when you want `/autosentry` available in
    every interactive session without re-running `skills install`.
    `agents` (AGENTS.md) is repo-specific and is silently skipped under
    `--scope global`.

    Existing files are skipped unless `--force` is passed.
    """
    if skill not in _VALID_SKILLS:
        console.print(
            f"[{ERR}]invalid --skill {skill!r}[/{ERR}] — choose from: {', '.join(_VALID_SKILLS)}"
        )
        raise typer.Exit(code=2)
    if scope not in _VALID_SCOPES:
        console.print(
            f"[{ERR}]invalid --scope {scope!r}[/{ERR}] — choose from: {', '.join(_VALID_SCOPES)}"
        )
        raise typer.Exit(code=2)

    tools_arg = _normalize_tools_arg(tool)
    skill_arg = cast(skills_mod.SkillName | str, skill)
    scope_arg = cast(skills_mod.Scope, scope)

    try:
        results = skills_mod.install(
            target_dir=target,
            tools=tools_arg,
            skill_name=skill_arg,  # type: ignore[arg-type]
            scope=scope_arg,
            force=force,
        )
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[{ERR}]error:[/{ERR}] {e}")
        raise typer.Exit(code=1) from e

    location = "$HOME" if scope == "global" else str(target)
    t = Table(
        title=f"skill={skill}  scope={scope}  →  {location}",
        show_header=True,
        header_style="bold",
    )
    t.add_column("tool")
    t.add_column("skill")
    t.add_column("destination")
    t.add_column("status")
    for r in results:
        color = {
            "created": OK,
            "overwritten": WARN,
            "skipped_exists": "dim",
            "skipped_no_global": "dim",
        }[r.status]
        if scope == "global" and r.target.global_destination is not None:
            shown = str(r.target.global_destination)
        else:
            shown = (
                str(r.written.relative_to(target.resolve()))
                if r.written.is_relative_to(target.resolve())
                else str(r.written)
            )
        t.add_row(r.target.tool, r.target.skill, shown, f"[{color}]{r.status}[/{color}]")
    console.print(t)

    skipped = [r for r in results if r.status == "skipped_exists"]
    if skipped:
        console.print(
            f"[dim]{len(skipped)} file(s) already existed — pass --force to overwrite.[/dim]"
        )
    no_global = [r for r in results if r.status == "skipped_no_global"]
    if no_global:
        names = ", ".join(r.target.tool for r in no_global)
        console.print(
            f"[dim]{len(no_global)} tool(s) have no global path ({names}) — "
            f"skipped under --scope global.[/dim]"
        )


@skills_app.command("list")
def skills_list() -> None:
    """Show every (tool, skill) pair `skills install` can target.

    Lists the tool, the skill (autosentry vs init), the local
    destination (relative to repo root), and the global destination (in
    $HOME). A blank global cell means the tool has no global path —
    only `--scope local` will write that one.
    """
    t = Table(title="autosentry skill targets", show_header=True, header_style="bold")
    t.add_column("tool")
    t.add_column("skill")
    t.add_column("local destination")
    t.add_column("global destination")
    t.add_column("description")
    for target in skills_mod.targets_for(None, skill_name="all"):
        global_dest = str(target.global_destination) if target.global_destination else "—"
        t.add_row(
            target.tool,
            target.skill,
            str(target.local_destination),
            global_dest,
            target.label,
        )
    console.print(t)


def _normalize_tools_arg(tool: list[str]) -> list[skills_mod.ToolName] | None:
    """Translate CLI ``--tool`` repeats into the skills module's enum list."""
    if not tool or tool == ["all"]:
        return None
    valid = set(skills_mod.ALL_TOOLS)
    unknown = [t for t in tool if t != "all" and t not in valid]
    if unknown:
        msg = f"unknown tool(s): {', '.join(unknown)} — choose from {', '.join(sorted(valid))}"
        raise typer.BadParameter(msg)
    if "all" in tool:
        return None
    return [t for t in tool if t in valid]  # type: ignore[misc]
