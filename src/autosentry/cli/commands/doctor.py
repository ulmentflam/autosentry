"""``autosentry doctor`` — environment health check.

One-shot diagnostic that confirms the bits autosentry depends on are
healthy: the CLI itself is on PATH, ``git`` is present, the cwd is a
git repo, ``autosentry.yaml`` parses, tree-sitter grammars load for
the declared languages, the Claude CLI is on PATH if the user has
healing enabled, etc.

Output is a rich table — green for healthy, yellow for warnings, red
for things that block normal operation. Exit code 0 if everything
checks out, 1 if any check is red.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import typer
from rich.table import Table

from autosentry import __version__
from autosentry.cli import app
from autosentry.cli.style import ACCENT, DIM, ERR, OK, WARN, console
from autosentry.config import DEFAULT_CONFIG_PATH

CheckStatus = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    detail: str


@app.command()
def doctor(
    config: Path = typer.Option(  # noqa: B008
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help="Path to the config (defaults to .autosentry/autosentry.yaml; "
        "skipped checks if missing).",
    ),
) -> None:
    """Audit the autosentry environment and print a pass/warn/fail table.

    Checks: the CLI is on PATH and its version; `git` is installed
    and the cwd is a repo (fix branches need this); `autosentry.yaml`
    parses; tree-sitter grammars load for every declared language;
    the Claude healer's resolved mode (subprocess vs interactive vs
    rule-only); the healer-aware restart budget (escalation +
    give-up thresholds); and `.autosentry/state.json` sanity if the
    monitor has started.

    Exits 0 when nothing is red. Warnings (yellow) don't block
    operation but may degrade features.
    """
    checks: list[Check] = []
    checks.append(_check_self_version())
    checks.append(_check_git_available())
    cwd = Path.cwd()
    checks.append(_check_git_repo(cwd))
    config_path, cfg = _check_config_loadable(config)
    checks.append(config_path)
    if cfg is not None:
        checks.append(_check_tree_sitter(cfg))
        checks.append(_check_claude_cli(cfg))
        checks.append(_check_healer_budget(cfg))
        checks.append(_check_state_file(cfg))

    _render(checks)
    if any(c.status == "fail" for c in checks):
        raise typer.Exit(code=1)


# ----- checks ---------------------------------------------------------------


def _check_self_version() -> Check:
    return Check(name="autosentry", status="ok", detail=f"v{__version__}")


def _check_git_available() -> Check:
    if shutil.which("git") is None:
        return Check(name="git", status="warn", detail="not on PATH — fix branches disabled")
    out = _capture(["git", "--version"])
    return Check(name="git", status="ok", detail=out)


def _check_git_repo(cwd: Path) -> Check:
    result = _run(["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"])
    if result.returncode != 0:
        return Check(
            name="git repo",
            status="warn",
            detail="cwd is not a git repo — fix branches won't be created",
        )
    branch = _capture(["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"])
    return Check(name="git repo", status="ok", detail=f"branch: {branch}")


def _check_config_loadable(config: Path) -> tuple[Check, object | None]:
    from autosentry.config import load_config, resolve_existing_config

    found = resolve_existing_config(config)
    if found is None:
        return (
            Check(
                name="autosentry.yaml",
                status="warn",
                detail=f"{config} not found — run `autosentry init`",
            ),
            None,
        )
    try:
        cfg = load_config(found)
    except Exception as e:  # noqa: BLE001
        return (
            Check(name="autosentry.yaml", status="fail", detail=f"parse error: {e}"),
            None,
        )
    return Check(name="autosentry.yaml", status="ok", detail=str(found)), cfg


def _check_tree_sitter(cfg) -> Check:  # noqa: ANN001 — AutoSentryConfig
    langs = list(cfg.source_explode.languages)
    if not langs:
        return Check(name="tree-sitter grammars", status="ok", detail="no languages declared")
    try:
        importlib.import_module("tree_sitter_language_pack")
    except ImportError as e:
        return Check(
            name="tree-sitter grammars",
            status="fail",
            detail=f"tree_sitter_language_pack not importable: {e}",
        )
    failures: list[str] = []
    from tree_sitter_language_pack import get_parser  # type: ignore[import-untyped]

    for lang in langs:
        try:
            get_parser(lang)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{lang}: {e}")
    if failures:
        return Check(
            name="tree-sitter grammars",
            status="warn",
            detail="; ".join(failures),
        )
    return Check(
        name="tree-sitter grammars",
        status="ok",
        detail=f"loaded {len(langs)} grammar(s): {', '.join(langs)}",
    )


def _check_claude_cli(cfg) -> Check:  # noqa: ANN001
    """Resolved healer mode — covers enabled/mode/subagent presence.

    The Claude healer has two modes (subprocess + interactive) and both
    are valid. We resolve which one would actually fire and report that,
    instead of treating "no `claude` on PATH" as an automatic fail.
    """
    claude_cfg = cfg.healing.claude
    if claude_cfg.enabled is False:
        return Check(name="claude healer", status="ok", detail="disabled in config")

    skill_present = _skill_present(cfg)
    binary = (claude_cfg.command or ["claude"])[0]
    cli_present = shutil.which(binary) is not None

    configured_mode = claude_cfg.mode
    if configured_mode == "subprocess":
        if cli_present:
            return Check(name="claude healer", status="ok", detail=f"subprocess ({binary} on PATH)")
        return Check(
            name="claude healer",
            status="fail",
            detail=f"mode=subprocess but {binary!r} is not on PATH",
        )
    if configured_mode == "interactive":
        if skill_present:
            return Check(
                name="claude healer",
                status="ok",
                detail="interactive (/autosentry skill installed)",
            )
        return Check(
            name="claude healer",
            status="fail",
            detail="mode=interactive but no /autosentry skill installed",
        )
    # auto
    if skill_present:
        return Check(
            name="claude healer",
            status="ok",
            detail="auto → interactive (/autosentry skill installed)",
        )
    if cli_present:
        return Check(
            name="claude healer", status="ok", detail=f"auto → subprocess ({binary} on PATH)"
        )
    return Check(
        name="claude healer",
        status="warn",
        detail=(
            "auto → rule-only (no skill installed and no `claude` on PATH); "
            "run `autosentry skills install --tool claude`"
        ),
    )


def _skill_present(cfg) -> bool:  # noqa: ANN001
    """Mirror ClaudeHealer._SKILL_MARKERS without re-importing private state."""
    repo_root = cfg.resolve(".")
    markers = (
        ".claude/commands/autosentry.md",
        ".opencode/command/autosentry.md",
        ".codex/prompts/autosentry.md",
        ".gemini/commands/autosentry.toml",
        ".cursor/commands/autosentry.md",
        "AGENTS.md",
    )
    return any((repo_root / m).exists() for m in markers)


def _check_healer_budget(cfg) -> Check:  # noqa: ANN001
    """Surface the healer-aware restart budget so an operator can see
    when force-Claude escalation will kick in before exhaustion."""
    max_restarts = cfg.process.restart_policy.max_restarts
    explicit = cfg.healing.escalate_to_claude_after
    threshold = explicit if explicit is not None and explicit > 0 else max(1, max_restarts // 2)
    detail = (
        f"escalate to Claude at {threshold}/{max_restarts} unverified restarts · "
        f"give up at {max_restarts}/{max_restarts}"
    )
    return Check(name="healer budget", status="ok", detail=detail)


def _check_state_file(cfg) -> Check:  # noqa: ANN001
    path = cfg.resolve(cfg.state_path)
    if not path.exists():
        return Check(name="state.json", status="ok", detail="not started yet (no state file)")
    try:
        import json as _json

        data = _json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return Check(name="state.json", status="fail", detail=f"unreadable: {e}")
    pid = data.get("pid")
    last = data.get("last_heartbeat")
    restarts = data.get("restarts", 0)
    total = data.get("restarts_total", restarts)
    return Check(
        name="state.json",
        status="ok",
        detail=(f"pid={pid} last_heartbeat={last} unverified={restarts} all_time={total}"),
    )


# ----- rendering ------------------------------------------------------------


def _render(checks: list[Check]) -> None:
    t = Table(
        title=f"[{ACCENT}]autosentry doctor[/{ACCENT}]", show_header=True, header_style="bold"
    )
    t.add_column("check")
    t.add_column("status", justify="center")
    t.add_column("detail")
    counts = {"ok": 0, "warn": 0, "fail": 0}
    for c in checks:
        counts[c.status] += 1
        glyph = {
            "ok": f"[{OK}]✓[/{OK}]",
            "warn": f"[{WARN}]![/{WARN}]",
            "fail": f"[{ERR}]✗[/{ERR}]",
        }[c.status]
        t.add_row(c.name, glyph, c.detail)
    console.print(t)
    summary = (
        f"[{OK}]{counts['ok']} ok[/{OK}]  "
        f"[{WARN}]{counts['warn']} warn[/{WARN}]  "
        f"[{ERR}]{counts['fail']} fail[/{ERR}]"
    )
    console.print(summary)
    if counts["fail"]:
        console.print(
            f"[{DIM}]fix the failing checks above before running `autosentry run`.[/{DIM}]"
        )
    elif counts["warn"]:
        console.print(f"[{DIM}]warnings don't block operation but may degrade features.[/{DIM}]")
    else:
        console.print(f"[{DIM}]all clear — `autosentry run` should work.[/{DIM}]")


# ----- subprocess helpers ---------------------------------------------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, timeout=10, check=False
    )


def _capture(cmd: list[str]) -> str:
    r = _run(cmd)
    return r.stdout.strip() if r.returncode == 0 else ""
