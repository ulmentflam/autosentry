"""``autosentry init`` — scaffold the ``.autosentry/`` tree into a repo.

The config lives at ``.autosentry/autosentry.yaml`` alongside the runtime
state, so a single ``.autosentry/`` git-ignore entry (written by init)
covers everything. The interactive flow detects the user's stack
(python/node/go/rust) and
offers sensible defaults for ``process.command``. Pass
``--non-interactive`` to skip prompts (preserves the original behavior
for scripts / CI). Pass ``--upgrade`` to refresh defaults in an
existing config without losing customizations.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import typer
from ruamel.yaml import YAML

from autosentry.cli import app
from autosentry.cli.style import (
    ACCENT,
    DIM,
    INFO,
    OK,
    WARN,
    ask,
    confirm,
    console,
    hint,
    is_interactive,
)
from autosentry.config import DEFAULT_CONFIG_PATH, LEGACY_CONFIG_PATH

# Templates live next to the package — same path the legacy CLI used.
TEMPLATES = Path(__file__).resolve().parents[2] / "templates"

# Leaf paths the ``--upgrade`` flow refreshes from the template. Only
# tuning knobs go here — never user-personal data like ``process.command``,
# ``process.env``, ``config_snapshots``, ``detectors``, ``rules``, or
# ``notifiers``. Add a path here when its default changes between
# releases so users can pick up the new value without manually diffing.
_UPGRADEABLE_PATHS: tuple[tuple[str, ...], ...] = (
    ("process", "restart_policy", "max_restarts"),
    ("process", "restart_policy", "cooldown_seconds"),
    ("monitor", "poll_interval_seconds"),
    ("monitor", "stall_timeout_seconds"),
    ("monitor", "log_tail_lines"),
    ("monitor", "log_excerpt_lines"),
    ("source_explode", "context_lines"),
    ("source_explode", "max_frames"),
    ("healing", "verify_window_seconds"),
    ("healing", "escalate_to_claude_after"),
    ("healing", "regression_action"),
    ("healing", "budget", "max_attempts_per_detector_per_hour"),
    ("healing", "budget", "max_wall_seconds_per_incident"),
    ("healing", "claude", "enabled"),
    ("healing", "claude", "mode"),
    ("healing", "claude", "timeout_seconds"),
    ("healing", "git", "branch_prefix"),
    ("healing", "git", "auto_merge"),
)


@app.command()
def init(
    target: Path = typer.Argument(  # noqa: B008
        Path("."), help="Directory to initialize (default: cwd)"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=("Overwrite an existing autosentry.yaml (or skip prompts during --upgrade)."),
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Skip prompts; write defaults verbatim (CI-friendly).",
    ),
    for_agent: bool = typer.Option(
        False,
        "--for-agent",
        help="Drop .autosentry/AGENT_NOTES.md with a structured onboarding playbook.",
    ),
    upgrade: bool = typer.Option(
        False,
        "--upgrade",
        help=(
            "Refresh tuning defaults in an existing autosentry.yaml from the "
            "current template. Preserves comments and customizations; only "
            "scalar tuning keys are touched. Pair with --force to skip "
            "per-key prompts."
        ),
    ),
    install_skills: str = typer.Option(
        "ask",
        "--install-skills",
        help=(
            "Install the /autosentry slash command alongside init. "
            "Choices: 'none' (skip), 'local' (this repo), 'global' (the user's "
            "home — every repo inherits), 'ask' (prompt interactively; the "
            "default). 'ask' falls back to 'none' in non-interactive sessions."
        ),
    ),
) -> None:
    """Scaffold the ``.autosentry/`` tree — a heavily-commented
    ``.autosentry/autosentry.yaml`` plus the runtime dirs and a
    ``.autosentry/.gitignore`` — into the target directory.

    Run this once per repo. On an interactive terminal the command
    detects your stack (pyproject.toml / package.json / Cargo.toml /
    go.mod) and offers a starting process.command + config_snapshots,
    then asks whether to install the /autosentry slash command for
    your AI editor (and at what scope — this repo or every repo).
    Add --non-interactive to skip prompts (CI).

    --install-skills accepts: 'none' (skip), 'local' (this repo),
    'global' (user's home), or 'ask' (the default — prompt in
    interactive sessions, fall back to 'none' otherwise).

    After init, run `autosentry doctor` to confirm the environment
    looks healthy, then `autosentry run`.

    Use --upgrade to refresh stale scalar defaults in an existing
    autosentry.yaml without losing customizations; pair with --force
    to skip per-key prompts.
    """
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)

    autosentry_dir = target / ".autosentry"
    yaml_dst = target / DEFAULT_CONFIG_PATH  # target/.autosentry/autosentry.yaml
    legacy_dst = target / LEGACY_CONFIG_PATH  # pre-0.8 root-level config
    yaml_src = TEMPLATES / "autosentry.yaml.tmpl"

    # ``--upgrade`` is a different flow from a fresh init: refresh scalar
    # defaults in the existing config in place, then exit. Prefer the new
    # location; fall back to — and migrate — a legacy root-level config.
    if upgrade:
        upgrade_target = yaml_dst if yaml_dst.exists() else legacy_dst
        if not upgrade_target.exists():
            console.print(
                f"[{WARN}]nothing to upgrade — no config at {yaml_dst} "
                f"(or legacy {legacy_dst}). Run `autosentry init` first.[/{WARN}]"
            )
            raise typer.Exit(code=1)
        # Relocate a legacy root config into .autosentry/ (contents +
        # comments preserved) so every future run finds it canonically.
        if upgrade_target == legacy_dst:
            autosentry_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_dst), str(yaml_dst))
            _write_gitignore(autosentry_dir)
            console.print(f"  [{OK}]✓[/{OK}] migrated {legacy_dst} → {yaml_dst}")
            upgrade_target = yaml_dst
        changed = _upgrade_in_place(upgrade_target, yaml_src, force=force or non_interactive)
        if changed:
            console.print(f"[{OK}]✓[/{OK}] refreshed {len(changed)} key(s) in {upgrade_target}")
        else:
            hint("(no defaults needed refreshing)")
        return

    if yaml_dst.exists() and not force:
        # Interactive sessions get a choice instead of a hard exit — most of
        # the time the operator running `init` on top of an existing config
        # wanted to start over (a stale experiment, a half-built setup, etc.)
        # but didn't think to pass --force. Offer reset / upgrade / cancel.
        if is_interactive() and not non_interactive:
            choice = _prompt_existing_config_action(yaml_dst)
            if choice == "reset":
                force = True  # fall through to the fresh-init flow below
            elif choice == "upgrade":
                changed = _upgrade_in_place(yaml_dst, yaml_src, force=False)
                if changed:
                    console.print(f"[{OK}]✓[/{OK}] refreshed {len(changed)} key(s) in {yaml_dst}")
                else:
                    hint("(no defaults needed refreshing)")
                return
            else:
                console.print(f"[{DIM}](aborted; nothing changed)[/{DIM}]")
                raise typer.Exit(code=1)
        else:
            console.print(f"[{WARN}]config already exists at {yaml_dst}[/{WARN}]")
            console.print("Pass --force to overwrite, or --upgrade to refresh defaults in place.")
            raise typer.Exit(code=1)
    # A pre-0.8 root-level config would be silently shadowed by a fresh one
    # written here. Don't clobber it — point the user at --upgrade (migrates
    # it) or --force (start clean in the new location).
    if legacy_dst.exists() and not yaml_dst.exists() and not force:
        console.print(f"[{WARN}]found a root-level {legacy_dst.name} (pre-0.8 layout)[/{WARN}]")
        console.print(
            "Run `autosentry init --upgrade` to migrate it into .autosentry/, "
            "or --force to start fresh there."
        )
        raise typer.Exit(code=1)

    autosentry_dir.mkdir(parents=True, exist_ok=True)
    (autosentry_dir / "incidents").mkdir(exist_ok=True)
    (autosentry_dir / "logs").mkdir(exist_ok=True)
    prompts = autosentry_dir / "prompts"
    prompts.mkdir(exist_ok=True)
    _write_gitignore(autosentry_dir)
    shutil.copy(yaml_src, yaml_dst)
    shutil.copy(TEMPLATES / "recovery.md.tmpl", prompts / "recovery.md")

    program_dst = autosentry_dir / "program.md"
    if not program_dst.exists():
        shutil.copy(TEMPLATES / "program.md.tmpl", program_dst)

    # Interactive enhancement: amend autosentry.yaml with detected defaults.
    if is_interactive() and not non_interactive:
        try:
            _interactive_fill(yaml_dst, target)
        except (KeyboardInterrupt, EOFError):
            hint("(skipped interactive setup)")

    # Skills install: a non-interactive flag picks the scope outright;
    # 'ask' is interactive (defaults to 'none' when stdin isn't a TTY).
    chosen_scope = _resolve_install_skills(install_skills, non_interactive)
    if chosen_scope in ("local", "global"):
        _run_skills_install(target, scope=chosen_scope)

    if for_agent:
        _write_agent_notes(target)

    _print_next_steps(yaml_dst, target, program_dst, for_agent=for_agent)


# ----- upgrade flow ---------------------------------------------------------


def _upgrade_in_place(
    yaml_path: Path, template_path: Path, *, force: bool
) -> list[tuple[tuple[str, ...], Any, Any]]:
    """Refresh scalar defaults in ``yaml_path`` from ``template_path``.

    Returns the list of ``(path, old_value, new_value)`` actually applied.
    ``force`` skips the per-key prompt and applies every diff (and inserts
    any missing keys). When prompts are shown, "y" applies, "n" skips,
    "a" applies the rest without further asking.
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    with yaml_path.open("r", encoding="utf-8") as f:
        user_data = yaml.load(f)
    with template_path.open("r", encoding="utf-8") as f:
        template_data = yaml.load(f)
    if user_data is None or template_data is None:
        return []

    applied: list[tuple[tuple[str, ...], Any, Any]] = []
    apply_all = force
    for path in _UPGRADEABLE_PATHS:
        new_value = _get_nested(template_data, path)
        if new_value is None:
            # Template doesn't define it (e.g. removed in a later
            # release); skip without touching the user's file.
            continue
        old_value = _get_nested(user_data, path)
        if old_value == new_value:
            continue
        # Render a one-line diff for the prompt.
        loc = ".".join(path)
        if old_value is None:
            change_label = f"[{INFO}]+[/{INFO}] {loc} = {new_value!r} [{DIM}](new key)[/{DIM}]"
        else:
            change_label = (
                f"[{ACCENT}]~[/{ACCENT}] {loc}: "
                f"[{WARN}]{old_value!r}[/{WARN}] → [{OK}]{new_value!r}[/{OK}]"
            )
        console.print(change_label)
        if not apply_all:
            choice = ask("apply? [y]es / [n]o / [a]ll", default="y").strip().lower()
            if choice.startswith("a"):
                apply_all = True
            elif choice.startswith("n"):
                continue
        _set_nested(user_data, path, new_value)
        applied.append((path, old_value, new_value))

    if applied:
        # Atomic write to dodge partial-rewrite races.
        tmp = yaml_path.with_suffix(yaml_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            yaml.dump(user_data, f)
        tmp.replace(yaml_path)
    return applied


def _get_nested(data: Any, path: tuple[str, ...]) -> Any:
    cur = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _set_nested(data: Any, path: tuple[str, ...], value: Any) -> None:
    cur = data
    for key in path[:-1]:
        if key not in cur or not isinstance(cur[key], dict):
            cur[key] = {}
        cur = cur[key]
    cur[path[-1]] = value


# ----- interactive flow ----------------------------------------------------


def _interactive_fill(yaml_path: Path, repo_root: Path) -> None:
    """Walk the user through filling in the most important config keys.

    We rewrite the YAML in place via ruamel so the heavily-commented
    template's comments survive.
    """
    console.print(f"\n[{ACCENT}]autosentry setup[/{ACCENT}] — quick interactive walkthrough\n")
    suggested_cmd = _suggest_command(repo_root)
    if suggested_cmd:
        console.print(
            f"  [{DIM}]detected stack →[/{DIM}] suggested command: "
            f"[bold]{' '.join(suggested_cmd)}[/bold]"
        )

    cmd_str = ask(
        "What command should autosentry supervise? (space-separated argv)",
        default=" ".join(suggested_cmd) if suggested_cmd else "python your_script.py",
    )
    if not cmd_str.strip():
        return
    cmd_tokens = cmd_str.split()

    snapshots = _propose_snapshots(repo_root)
    if snapshots:
        listing = ", ".join(snapshots)
        if confirm(
            f"Snapshot these config files on every incident? [{listing}]",
            default=True,
        ):
            chosen_snapshots = snapshots
        else:
            chosen_snapshots = []
    else:
        chosen_snapshots = []

    yaml = YAML()
    yaml.preserve_quotes = True
    with yaml_path.open("r", encoding="utf-8") as f:
        data = yaml.load(f)
    if data is None:
        return
    data.setdefault("process", {})["command"] = cmd_tokens
    if chosen_snapshots:
        data["config_snapshots"] = chosen_snapshots
    with yaml_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)
    console.print(f"  [{OK}]✓[/{OK}] wrote process.command + config_snapshots\n")


def _suggest_command(repo_root: Path) -> list[str] | None:
    """Best-effort detection of what to supervise based on cwd contents."""
    if (repo_root / "pyproject.toml").exists() or (repo_root / "setup.py").exists():
        return ["python", "your_script.py"]
    if (repo_root / "package.json").exists():
        return ["npm", "start"]
    if (repo_root / "Cargo.toml").exists():
        return ["cargo", "run"]
    if (repo_root / "go.mod").exists():
        return ["go", "run", "."]
    if (repo_root / "Gemfile").exists():
        return ["bundle", "exec", "rails", "server"]
    return None


def _propose_snapshots(repo_root: Path) -> list[str]:
    """Files commonly worth capturing in every incident folder."""
    candidates = [
        ".env",
        "configs/run.yaml",
        "configs/train.yaml",
        "config.yaml",
        "pyproject.toml",
        "package.json",
    ]
    return [c for c in candidates if (repo_root / c).exists()]


# ----- gitignore ------------------------------------------------------------


def _write_gitignore(autosentry_dir: Path) -> None:
    """Drop a ``.gitignore`` that keeps the whole ``.autosentry/`` tree —
    config and runtime state alike — out of version control.

    Self-contained: one file inside ``.autosentry/``, zero edits to the
    repo's root ``.gitignore``. Delete it (or add ``!`` negations) to
    start tracking pieces of the tree. Never overwrites an existing file.
    """
    gi = autosentry_dir / ".gitignore"
    if gi.exists():
        return
    gi.write_text(
        "# Created by `autosentry init`. Everything autosentry writes lives\n"
        "# under .autosentry/, so this ignores the whole tree — config and\n"
        "# runtime state alike. Remove this file (or add `!` negations) to\n"
        "# track pieces of it in git.\n"
        "*\n",
        encoding="utf-8",
    )


# ----- agent notes ---------------------------------------------------------


def _write_agent_notes(repo_root: Path) -> None:
    """Drop a structured onboarding playbook AI agents can read on startup."""
    path = repo_root / ".autosentry" / "AGENT_NOTES.md"
    path.write_text(_AGENT_NOTES, encoding="utf-8")
    console.print(f"  [{OK}]✓[/{OK}] wrote {path}")


_AGENT_NOTES = """\
# AGENT_NOTES — autosentry has been initialized in this repo

If you (an AI agent) are reading this, autosentry is installed and the
repo is configured. Your job is to operate the supervisor end-to-end.
See `AGENTS.md` at the repo root for the full playbook. The condensed
runbook for *this* repo:

1. Read `.autosentry/autosentry.yaml`. Verify `process.command` matches the actual
   thing the user wants supervised. If empty / placeholder, prompt the
   user before launching.
2. Run `autosentry doctor` to confirm the environment is healthy.
3. Start the monitor in the background:
   `nohup autosentry run > /dev/null 2>&1 &`
4. Tail the structured log:
   `tail -F .autosentry/logs/autosentry.log`
5. When a detector fires, read `.autosentry/incidents/<latest>/report.md`.
6. After three Claude-fixed incidents for the same detector all `kept`,
   propose a YAML rule that codifies the recurring fix.

Use `autosentry analyze --since 24h` to summarize the attempts ledger.

Never delete `.autosentry/state.json` or `.autosentry/attempts.tsv`.
"""


# ----- next-steps panel ----------------------------------------------------


def _print_next_steps(yaml_dst: Path, target: Path, program_dst: Path, *, for_agent: bool) -> None:
    state_path = target / ".autosentry" / "state.json"
    files_lines = [
        f"  config:  [bold]{yaml_dst}[/bold]",
        f"  state:   {state_path}",
        f"  program: {program_dst}",
    ]
    if for_agent:
        files_lines.append(f"  notes:   {target}/.autosentry/AGENT_NOTES.md")
    next_lines = [
        "  1. Review .autosentry/autosentry.yaml — detectors and rules are starter values.",
        "  2. Run [bold]autosentry doctor[/bold] to confirm the environment is healthy.",
        "  3. Start the monitor: [bold]autosentry run[/bold]",
        "  4. Operator dashboard: [bold]autosentry watch[/bold] · "
        "Incident browser: [bold]autosentry web[/bold]",
        f"  5. AI editor skills: [bold]autosentry skills install --tool all[/bold] "
        f"[{DIM}](drops /autosentry into Claude/Cursor/Codex/etc.)[/{DIM}]",
    ]
    # In interactive mode wrap the summary in a panel so it visually
    # detaches from the prompts above it. In non-interactive / piped
    # output the panel still renders but the colors auto-strip.
    from rich.panel import Panel

    body = (
        f"[{OK}]✓[/{OK}] initialized autosentry at [bold]{target}[/bold]\n"
        + "\n".join(files_lines)
        + f"\n\n[{ACCENT}]next[/{ACCENT}]\n"
        + "\n".join(next_lines)
    )
    console.print(Panel(body, border_style=OK, padding=(1, 2)))


# ----- skills install offer ------------------------------------------------


_VALID_INSTALL_CHOICES = ("none", "local", "global", "ask")


def _resolve_install_skills(value: str, non_interactive: bool) -> str:
    """Pick the effective scope for the skills-install offer.

    ``ask`` is the default: prompt the user when the session is
    interactive, fall back to ``none`` otherwise so scripts don't
    silently install global skills.
    """
    if value not in _VALID_INSTALL_CHOICES:
        console.print(
            f"[{WARN}]invalid --install-skills {value!r}[/{WARN}] — "
            f"falling back to 'none' (choices: {', '.join(_VALID_INSTALL_CHOICES)})"
        )
        return "none"
    if value != "ask":
        return value
    if non_interactive or not is_interactive():
        return "none"

    console.print(
        f"\n[{ACCENT}]AI editor skills[/{ACCENT}] — install the [bold]/autosentry[/bold] "
        f"slash command so your editor (Claude / Cursor / Codex / Gemini / OpenCode / "
        f"Aider / Continue / Windsurf / Zed) gets the playbook in every session."
    )
    if not confirm("Install /autosentry now?", default=True):
        hint("  (skipped; run `autosentry skills install` later when ready)")
        return "none"

    console.print(
        f"  [{INFO}]local[/{INFO}]  — drops files into [bold]this repo[/bold]\n"
        f"           e.g. [{DIM}].claude/commands/autosentry.md, .cursor/commands/autosentry.md, "
        f"AGENTS.md, …[/{DIM}]\n"
        f"  [{INFO}]global[/{INFO}] — drops files into [bold]your home[/bold] "
        f"(every repo inherits)\n"
        f"           e.g. [{DIM}]~/.claude/commands/autosentry.md, ~/.codex/prompts/autosentry.md, "
        f"~/.gemini/commands/autosentry.toml, …[/{DIM}]"
    )
    scope = (
        ask("Scope — [l]ocal (this repo) or [g]lobal (every repo)?", default="local")
        .strip()
        .lower()
    )
    if scope.startswith("g"):
        return "global"
    return "local"


def _run_skills_install(target: Path, *, scope: str) -> None:
    """Drop /autosentry skill wrappers for every supported tool.

    We always install the full ``autosentry`` skill (not the focused
    ``init`` skill) here — init is the entry point that the user just
    ran. Users who want the focused /autosentry-init slash command can
    run ``autosentry skills install --skill init`` separately.
    """
    # Lazy import to avoid the cycle: cli/__init__ imports cli/commands,
    # and skills_mod is fine, but pulling it at module load isn't worth
    # the indirection.
    from autosentry import skills as skills_mod

    try:
        results = skills_mod.install(
            target_dir=target,
            tools=None,  # all tools
            skill_name="autosentry",
            scope=scope,  # type: ignore[arg-type]
        )
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[{WARN}]skills install failed:[/{WARN}] {e}")
        return
    created = sum(1 for r in results if r.status == "created")
    skipped = sum(1 for r in results if r.status == "skipped_exists")
    no_global = sum(1 for r in results if r.status == "skipped_no_global")
    where = "$HOME" if scope == "global" else str(target)
    console.print(
        f"  [{OK}]✓[/{OK}] /autosentry skill installed at scope={scope} → {where} "
        f"[{DIM}](created {created}, skipped {skipped}, no-global {no_global})[/{DIM}]"
    )

    # Wire the session-dispatch Stop hook into .claude/settings.local.json.
    # Local scope only — global ``~/.claude/`` is per-user and shouldn't be
    # touched by an init in a single repo. Idempotent; safe to re-run.
    if scope == "local":
        _install_session_stop_hook(target)


def _prompt_existing_config_action(yaml_dst: Path) -> str:
    """Returns one of: 'reset' | 'upgrade' | 'cancel'.

    Default is 'reset' — the most common operator intent when re-running
    init on top of an existing config is "start over". 'upgrade' refreshes
    scalar defaults in place (the --upgrade flow). 'cancel' aborts.
    """
    console.print(f"\n[{WARN}]config already exists at {yaml_dst}[/{WARN}]")
    console.print(
        f"  [{INFO}]r[/{INFO}]eset    — overwrite with a fresh template, "
        f"re-run the interactive walkthrough\n"
        f"  [{INFO}]u[/{INFO}]pgrade  — refresh stale scalar defaults in place "
        f"(keep your edits)\n"
        f"  [{INFO}]c[/{INFO}]ancel   — leave it alone"
    )
    raw = ask("Choose [r]eset / [u]pgrade / [c]ancel", default="r").strip().lower()
    if raw.startswith("u"):
        return "upgrade"
    if raw.startswith("c"):
        return "cancel"
    return "reset"


def _install_session_stop_hook(target: Path) -> None:
    from autosentry.hooks import install_claude_stop_hook

    try:
        result = install_claude_stop_hook(target)
    except OSError as e:
        console.print(f"[{WARN}]Stop-hook install failed:[/{WARN}] {e}")
        return
    status_msg = {
        "created": "Claude Code Stop hook wired (settings.local.json created)",
        "added": "Claude Code Stop hook added to existing settings.local.json",
        "already_present": "Claude Code Stop hook already present",
        "skipped": "Claude Code settings.local.json unreadable — Stop hook NOT installed",
    }[result.status]
    style = WARN if result.status == "skipped" else OK
    marker = "✓" if result.status != "skipped" else "!"
    console.print(
        f"  [{style}]{marker}[/{style}] {status_msg} [{DIM}]({result.settings_path})[/{DIM}]"
    )
