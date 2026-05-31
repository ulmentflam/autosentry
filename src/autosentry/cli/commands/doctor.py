"""``autosentry doctor`` — environment health check + auto-repair.

One-shot diagnostic that confirms the bits autosentry depends on are
healthy and (with ``--fix``) repairs anything safe to repair without
operator intervention. Covers:

- **Layout & migration**: pre-0.8 root-level ``autosentry.yaml``,
  missing ``.autosentry/`` subdirs, missing vault directory, vault
  out of sync with ``incidents/index.jsonl``.
- **Corrupt state files**: ``state.json`` / ``attempts.tsv`` /
  ``index.jsonl`` parseability. Broken files get rotated aside to
  ``.broken-<ISO8601>`` so the next run starts clean.
- **Integration health**: Claude Code Stop hook when
  ``dispatch.mode: session``; missing provider API keys when
  ``healing.langgraph.enabled``; orphaned ``recovery_request.md``
  blocking the monitor.
- **Steering nudges**: surfaces deprecation warnings (e.g. legacy
  ``healing.claude.mode: subprocess``) with context.

Output: rich table — green for healthy, yellow for warnings, red
for things that block normal operation. Exit code 0 if everything
checks out, 1 if any check is red. With ``--fix``, exits 0 after
running every safe repair (then prints a final post-fix summary).
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import typer
from rich.table import Table

from autosentry import __version__
from autosentry.cli import app
from autosentry.cli.style import ACCENT, DIM, ERR, OK, WARN, console
from autosentry.config import DEFAULT_CONFIG_PATH, LEGACY_CONFIG_PATH

CheckStatus = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Repair:
    """A safe, idempotent fix doctor can run when ``--fix`` is passed.

    ``apply`` returns a short, human-readable summary of what it did
    (or what it would do, if a dry run is later added). It must not
    raise; surface failures by returning a string starting with
    ``"FAILED: "``.
    """

    description: str
    apply: Callable[[], str]


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    detail: str
    repair: Repair | None = None


@dataclass
class _RepairResult:
    check_name: str
    message: str


@app.command()
def doctor(
    config: Path = typer.Option(  # noqa: B008
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help="Path to the config (defaults to .autosentry/autosentry.yaml; "
        "skipped checks if missing).",
    ),
    fix: bool = typer.Option(
        False,
        "--fix",
        help=(
            "Run every available auto-repair for warn/fail checks. Safe and "
            "idempotent: broken files get rotated to .broken-<timestamp>, "
            "missing dirs recreated, Stop hook installed, vault rebuilt. "
            "Won't change anything sensitive (API keys, config edits)."
        ),
    ),
) -> None:
    """Audit the autosentry environment and (with ``--fix``) repair common issues.

    Twelve+ categories of check spanning installer, layout, broken
    state files, integration health, and steering. Exit 0 when nothing
    is red; 1 otherwise. With ``--fix``, applies all safe repairs first
    and then re-renders the table — exit reflects the post-fix state.
    """
    checks = _run_all_checks(config)

    if fix:
        repaired = _apply_repairs(checks)
        if repaired:
            console.print(f"[{ACCENT}]applied {len(repaired)} repair(s):[/{ACCENT}]")
            for r in repaired:
                console.print(f"  [{OK}]✓[/{OK}] {r.check_name}: {r.message}")
            console.print(f"[{DIM}]re-running checks to confirm…[/{DIM}]\n")
            checks = _run_all_checks(config)

    _render(checks)
    if any(c.status == "fail" for c in checks):
        raise typer.Exit(code=1)


# ----- orchestration --------------------------------------------------------


def _run_all_checks(config: Path) -> list[Check]:
    checks: list[Check] = []
    checks.append(_check_self_version())
    checks.append(_check_git_available())
    cwd = Path.cwd()
    checks.append(_check_git_repo(cwd))
    # Migration: surface legacy root-level config before we try to load.
    legacy = _check_legacy_root_config(cwd)
    if legacy is not None:
        checks.append(legacy)
    config_path_check, cfg = _check_config_loadable(config)
    checks.append(config_path_check)
    if cfg is not None:
        # Layout
        checks.append(_check_autosentry_dir_structure(cfg))
        checks.append(_check_vault_directory(cfg))
        # Corrupt files
        checks.append(_check_state_file(cfg))
        checks.append(_check_attempts_tsv(cfg))
        checks.append(_check_incidents_index(cfg))
        # Integration health
        checks.append(_check_stop_hook(cfg))
        checks.append(_check_provider_api_keys(cfg))
        checks.append(_check_orphan_recovery_request(cfg))
        # Steering
        checks.append(_check_claude_mode_subprocess(cfg))
        # Existing checks
        checks.append(_check_tree_sitter(cfg))
        checks.append(_check_claude_cli(cfg))
        checks.append(_check_healer_budget(cfg))
    return checks


def _apply_repairs(checks: list[Check]) -> list[_RepairResult]:
    """Run every repair whose check is warn/fail. Returns the list of
    repairs actually applied. Each ``apply()`` is expected to be
    idempotent — running doctor twice with ``--fix`` is a no-op the
    second time."""
    results: list[_RepairResult] = []
    for c in checks:
        if c.repair is None or c.status == "ok":
            continue
        try:
            msg = c.repair.apply()
        except Exception as e:  # noqa: BLE001
            msg = f"FAILED: {type(e).__name__}: {e}"
        results.append(_RepairResult(check_name=c.name, message=msg))
    return results


# ----- baseline environment -------------------------------------------------


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


# ----- layout & migration ---------------------------------------------------


def _check_legacy_root_config(cwd: Path) -> Check | None:
    """Pre-0.8 layout stored ``autosentry.yaml`` at the repo root. Once
    a ``.autosentry/`` exists with the new-layout file, the legacy file
    is silently ignored — which can be confusing. Surface it so the user
    knows to migrate.

    Returns None when there's nothing legacy to report.
    """
    legacy = cwd / LEGACY_CONFIG_PATH
    new = cwd / DEFAULT_CONFIG_PATH
    if not legacy.exists():
        return None
    if new.exists():
        return Check(
            name="legacy config",
            status="warn",
            detail=(
                f"{legacy.name} exists alongside {new} — only the new path is loaded; "
                "delete the legacy file to silence this"
            ),
            repair=Repair(
                description=f"move {legacy} to .legacy backup",
                apply=lambda: _rotate_path(legacy, suffix=".legacy"),
            ),
        )
    # Legacy exists but no new — offer to migrate via init --upgrade.
    return Check(
        name="legacy config",
        status="warn",
        detail=(
            f"{legacy.name} is the pre-0.8 layout — run `autosentry init --upgrade` "
            "to relocate it into .autosentry/"
        ),
        repair=Repair(
            description="relocate legacy config into .autosentry/",
            apply=lambda: _migrate_legacy_config(legacy, new),
        ),
    )


def _check_autosentry_dir_structure(cfg) -> Check:  # noqa: ANN001
    """The .autosentry/ tree (incidents, logs, prompts) is created by
    ``init``; if the operator deleted it by hand, recreate the empty
    skeleton so the monitor doesn't surprise-crash on first write."""
    root = cfg.resolve(".") / ".autosentry"
    required = ["incidents", "logs", "prompts"]
    missing = [sub for sub in required if not (root / sub).is_dir()]
    if not missing:
        return Check(name=".autosentry tree", status="ok", detail="all subdirs present")
    return Check(
        name=".autosentry tree",
        status="warn",
        detail=f"missing: {', '.join(missing)}",
        repair=Repair(
            description="recreate missing .autosentry subdirs",
            apply=lambda: _recreate_subdirs(root, missing),
        ),
    )


def _check_vault_directory(cfg) -> Check:  # noqa: ANN001
    """When ``vault.enabled``, ensure the vault directory exists. If
    it doesn't but ``incidents/index.jsonl`` does, offer to rebuild
    the vault from existing incidents."""
    if not cfg.vault.enabled:
        return Check(name="vault dir", status="ok", detail="disabled in config")
    vault_root = cfg.resolve(cfg.vault.path)
    if vault_root.exists() and (vault_root / "index.md").exists():
        return Check(name="vault dir", status="ok", detail=str(vault_root))
    index_jsonl = cfg.resolve(cfg.incidents_dir) / "index.jsonl"
    if index_jsonl.exists():
        return Check(
            name="vault dir",
            status="warn",
            detail=f"missing {vault_root} — can rebuild from {index_jsonl.name}",
            repair=Repair(
                description="rebuild vault from incidents/index.jsonl",
                apply=lambda: _rebuild_vault(cfg),
            ),
        )
    return Check(
        name="vault dir",
        status="warn",
        detail=f"missing {vault_root} — will be created on next monitor start",
        repair=Repair(
            description="create empty vault scaffold",
            apply=lambda: _create_empty_vault(cfg),
        ),
    )


# ----- corrupt state files --------------------------------------------------


def _check_state_file(cfg) -> Check:  # noqa: ANN001
    path = cfg.resolve(cfg.state_path)
    if not path.exists():
        return Check(name="state.json", status="ok", detail="not started yet (no state file)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return Check(
            name="state.json",
            status="fail",
            detail=f"unreadable: {e} — rotate aside to start clean",
            repair=Repair(
                description="rotate corrupt state.json aside",
                apply=lambda: _rotate_path(path, suffix=".broken"),
            ),
        )
    pid = data.get("pid")
    last = data.get("last_heartbeat")
    restarts = data.get("restarts", 0)
    total = data.get("restarts_total", restarts)
    return Check(
        name="state.json",
        status="ok",
        detail=f"pid={pid} last_heartbeat={last} unverified={restarts} all_time={total}",
    )


def _check_attempts_tsv(cfg) -> Check:  # noqa: ANN001
    path = cfg.resolve(".") / ".autosentry" / "attempts.tsv"
    if not path.exists():
        return Check(name="attempts.tsv", status="ok", detail="no ledger yet")
    try:
        # Cheap sanity: every line should have at least 6 tab-separated
        # fields (timestamp, incident_id, detector, source, branch, status...).
        bad: list[int] = []
        with path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                if len(line.split("\t")) < 6:
                    bad.append(i)
        if bad:
            return Check(
                name="attempts.tsv",
                status="fail",
                detail=f"{len(bad)} malformed line(s) — rotate aside to start clean",
                repair=Repair(
                    description="rotate corrupt attempts.tsv aside",
                    apply=lambda: _rotate_path(path, suffix=".broken"),
                ),
            )
    except OSError as e:
        return Check(
            name="attempts.tsv",
            status="fail",
            detail=f"unreadable: {e}",
            repair=Repair(
                description="rotate unreadable attempts.tsv aside",
                apply=lambda: _rotate_path(path, suffix=".broken"),
            ),
        )
    return Check(name="attempts.tsv", status="ok", detail=f"{path}")


def _check_incidents_index(cfg) -> Check:  # noqa: ANN001
    path = cfg.resolve(cfg.incidents_dir) / "index.jsonl"
    if not path.exists():
        return Check(name="incidents/index.jsonl", status="ok", detail="no incidents yet")
    bad: list[int] = []
    total = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    bad.append(i)
    except OSError as e:
        return Check(
            name="incidents/index.jsonl",
            status="fail",
            detail=f"unreadable: {e}",
            repair=Repair(
                description="rotate unreadable index.jsonl aside",
                apply=lambda: _rotate_path(path, suffix=".broken"),
            ),
        )
    if bad:
        return Check(
            name="incidents/index.jsonl",
            status="fail",
            detail=f"{len(bad)} malformed line(s) of {total}",
            repair=Repair(
                description="rotate corrupt index.jsonl aside",
                apply=lambda: _rotate_path(path, suffix=".broken"),
            ),
        )
    return Check(name="incidents/index.jsonl", status="ok", detail=f"{total} incident(s) indexed")


# ----- integration health ---------------------------------------------------


def _check_stop_hook(cfg) -> Check:  # noqa: ANN001
    """When ``dispatch.mode: session``, the Claude Code Stop hook is
    what drives the session-watch loop. Missing hook = silently
    degraded experience."""
    if cfg.dispatch.mode != "session":
        return Check(name="stop hook", status="ok", detail="dispatch.mode is not 'session'")
    from autosentry.hooks import CLAUDE_SETTINGS_REL, STOP_HOOK_COMMAND

    settings_path = cfg.resolve(".") / CLAUDE_SETTINGS_REL
    if not settings_path.exists():
        return Check(
            name="stop hook",
            status="warn",
            detail=f"{settings_path} missing — session-watch won't fire",
            repair=Repair(
                description="install Claude Code Stop hook",
                apply=lambda: _install_stop_hook(cfg.resolve(".")),
            ),
        )
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError:
        return Check(
            name="stop hook",
            status="fail",
            detail=f"{settings_path} not parseable as JSON",
        )
    stop_groups = data.get("hooks", {}).get("Stop", []) or []
    found = any(
        isinstance(h, dict) and h.get("command") == STOP_HOOK_COMMAND
        for group in stop_groups
        if isinstance(group, dict)
        for h in (group.get("hooks") or [])
    )
    if found:
        return Check(name="stop hook", status="ok", detail="installed in settings.local.json")
    return Check(
        name="stop hook",
        status="warn",
        detail="settings.local.json present but hook not registered",
        repair=Repair(
            description="add Stop hook entry to settings.local.json",
            apply=lambda: _install_stop_hook(cfg.resolve(".")),
        ),
    )


def _check_provider_api_keys(cfg) -> Check:  # noqa: ANN001
    """Surface missing provider API keys for the LangGraph healer.
    No auto-repair — keys are sensitive and operator-managed."""
    if not cfg.healing.langgraph.enabled:
        return Check(name="langgraph api keys", status="ok", detail="healing.langgraph not enabled")
    import os as _os

    from autosentry.healers.langgraph_providers import PROVIDER_API_KEY_ENV

    primary = cfg.healing.langgraph.provider
    primary_env = PROVIDER_API_KEY_ENV[primary]
    missing: list[str] = []
    if not _os.environ.get(primary_env):
        missing.append(f"{primary_env} (primary provider={primary})")
    cc = cfg.healing.langgraph.cross_check
    if cc.enabled:
        cc_env = PROVIDER_API_KEY_ENV[cc.provider]
        if not _os.environ.get(cc_env):
            missing.append(f"{cc_env} (cross-check provider={cc.provider})")
    if missing:
        return Check(
            name="langgraph api keys",
            status="fail",
            detail=f"missing: {', '.join(missing)} — export and retry",
        )
    return Check(
        name="langgraph api keys",
        status="ok",
        detail=f"provider={primary} ({primary_env} set)",
    )


def _check_orphan_recovery_request(cfg) -> Check:  # noqa: ANN001
    """A stale ``recovery_request.md`` (no response, monitor not
    running) will make the next monitor block forever waiting on a
    response that already came in for a prior run. Rotate it aside."""
    req = cfg.resolve(cfg.healing.claude.request_path)
    resp = cfg.resolve(cfg.healing.claude.response_path)
    if not req.exists():
        return Check(name="recovery request", status="ok", detail="none queued")
    # If the response file is newer, the handshake completed and the
    # request file is just leftover.
    req_mtime = req.stat().st_mtime
    resp_mtime = resp.stat().st_mtime if resp.exists() else 0.0
    state_path = cfg.resolve(cfg.state_path)
    state_age = 0.0
    if state_path.exists():
        import time as _t

        state_age = _t.time() - state_path.stat().st_mtime
    monitor_likely_dead = state_age > 600  # heartbeat older than 10m
    if resp_mtime > req_mtime:
        if monitor_likely_dead:
            return Check(
                name="recovery request",
                status="warn",
                detail="leftover request from a prior run — rotate aside before restarting",
                repair=Repair(
                    description="rotate stale recovery_request.md aside",
                    apply=lambda: _rotate_path(req, suffix=".stale"),
                ),
            )
        return Check(name="recovery request", status="ok", detail="response already in")
    if monitor_likely_dead:
        return Check(
            name="recovery request",
            status="warn",
            detail="open request with no response and monitor seems stopped",
            repair=Repair(
                description="rotate orphan recovery_request.md aside",
                apply=lambda: _rotate_path(req, suffix=".stale"),
            ),
        )
    return Check(name="recovery request", status="ok", detail="open and being processed")


# ----- steering -------------------------------------------------------------


def _check_claude_mode_subprocess(cfg) -> Check:  # noqa: ANN001
    """Steering nudge for the deprecated subprocess mode. The config-
    load warning fires once per run; doctor surfaces it explicitly so
    the operator has somewhere to read about the alternatives."""
    if cfg.healing.claude.mode != "subprocess":
        return Check(
            name="claude.mode steering",
            status="ok",
            detail=f"mode={cfg.healing.claude.mode}",
        )
    return Check(
        name="claude.mode steering",
        status="warn",
        detail=(
            "subprocess mode is per-call billed against your Anthropic API. "
            "Claude Code users: switch to dispatch.mode: session (free under "
            "your subscription). Headless deployments: switch to "
            "healing.claude.mode: langgraph (provider choice)."
        ),
    )


# ----- existing checks (carried over from <0.12.0) --------------------------


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


def _check_tree_sitter(cfg) -> Check:  # noqa: ANN001
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
    claude_cfg = cfg.healing.claude
    if claude_cfg.enabled is False and not cfg.healing.langgraph.enabled:
        return Check(name="claude healer", status="ok", detail="disabled in config")
    skill_present = _skill_present(cfg)
    binary = (claude_cfg.command or ["claude"])[0]
    cli_present = shutil.which(binary) is not None
    configured_mode = claude_cfg.mode
    if configured_mode == "langgraph":
        if cfg.healing.langgraph.enabled:
            return Check(
                name="claude healer",
                status="ok",
                detail=f"langgraph (provider={cfg.healing.langgraph.provider})",
            )
        return Check(
            name="claude healer",
            status="fail",
            detail="mode=langgraph but healing.langgraph.enabled is false",
        )
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
    if cfg.healing.langgraph.enabled:
        return Check(
            name="claude healer",
            status="ok",
            detail=f"auto → langgraph (provider={cfg.healing.langgraph.provider})",
        )
    if cli_present:
        return Check(
            name="claude healer", status="ok", detail=f"auto → subprocess ({binary} on PATH)"
        )
    return Check(
        name="claude healer",
        status="warn",
        detail=(
            "auto → rule-only (no skill, no langgraph, no `claude` on PATH); "
            "run `autosentry skills install --tool claude`"
        ),
    )


def _skill_present(cfg) -> bool:  # noqa: ANN001
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
    max_restarts = cfg.process.restart_policy.max_restarts
    explicit = cfg.healing.escalate_to_claude_after
    threshold = explicit if explicit is not None and explicit > 0 else max(1, max_restarts // 2)
    detail = (
        f"escalate to Claude at {threshold}/{max_restarts} unverified restarts · "
        f"give up at {max_restarts}/{max_restarts}"
    )
    return Check(name="healer budget", status="ok", detail=detail)


# ----- repair helpers (small, focused, idempotent) --------------------------


def _rotate_path(path: Path, *, suffix: str) -> str:
    """Move ``path`` aside to ``path.with_suffix(suffix + '.YYYY-MM-DDTHH-MM-SSZ')``.

    Idempotent: if the path doesn't exist, returns a no-op message.
    """
    if not path.exists():
        return "nothing to rotate (path already gone)"
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    dest = path.with_name(f"{path.name}{suffix}-{ts}")
    path.rename(dest)
    return f"moved to {dest.name}"


def _migrate_legacy_config(legacy: Path, new: Path) -> str:
    new.parent.mkdir(parents=True, exist_ok=True)
    if new.exists():
        return "destination already exists; left legacy in place"
    legacy.rename(new)
    return f"moved {legacy.name} -> {new}"


def _recreate_subdirs(root: Path, missing: list[str]) -> str:
    for sub in missing:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return f"created: {', '.join(missing)}"


def _install_stop_hook(target: Path) -> str:
    from autosentry.hooks import install_claude_stop_hook

    result = install_claude_stop_hook(target)
    return f"{result.status} ({result.settings_path})"


def _create_empty_vault(cfg) -> str:  # noqa: ANN001
    from autosentry.vault import VaultStore

    vault_root = cfg.resolve(cfg.vault.path)
    VaultStore(vault_root)  # constructor calls _ensure_layout
    return f"vault scaffold at {vault_root}"


def _rebuild_vault(cfg) -> str:  # noqa: ANN001
    """Walk ``incidents/index.jsonl`` and re-emit per-incident notes
    + the detector aggregator + (re-)run pattern detection. Doesn't
    touch the original ``incidents/`` folders — purely additive to the
    vault.

    Run notes are not rebuilt (we don't have the original start times);
    only incident- and pattern-level structure. Operators get the
    full graph back without the per-run framing.
    """
    from autosentry.vault import VaultStore
    from autosentry.vault.patterns import PatternIndex

    vault_root = cfg.resolve(cfg.vault.path)
    vault = VaultStore(vault_root)
    patterns = PatternIndex(
        vault_root / ".patterns.json",
        threshold=cfg.vault.pattern_threshold,
        similarity=cfg.vault.similarity_threshold,
    )
    index_path = cfg.resolve(cfg.incidents_dir) / "index.jsonl"
    n = 0
    with index_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            incident_id = entry.get("id")
            if not incident_id:
                continue
            classification = patterns.classify(
                incident_id=incident_id,
                detector=entry.get("detector", "unknown"),
                message=entry.get("message", ""),
                trace=None,
            )
            vault.record_incident(
                incident_id=incident_id,
                run_id="rebuilt",
                child_run_id=None,
                detector=entry.get("detector", "unknown"),
                kind=entry.get("kind", "anomaly"),
                message=entry.get("message", ""),
                fired_at=incident_id.split("-error-")[0].split("-anomaly-")[0],
                trace_hash=classification.trace_hash,
                pattern_slug=(
                    classification.pattern.slug if classification.pattern is not None else None
                ),
                incident_folder_rel=str(
                    (cfg.resolve(cfg.incidents_dir) / incident_id).relative_to(cfg.resolve("."))
                ),
            )
            if classification.pattern is not None:
                vault.record_pattern(
                    slug=classification.pattern.slug,
                    detector=classification.pattern.detector,
                    representative_message=classification.pattern.representative_message,
                    incident_ids=classification.pattern.incident_ids,
                    trace_hash=classification.pattern.trace_hash,
                )
            n += 1
    return f"replayed {n} incident(s) into the vault"


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
        detail = c.detail
        if c.repair is not None and c.status != "ok":
            detail += f"\n[{DIM}]repair: {c.repair.description} (pass --fix)[/{DIM}]"
        t.add_row(c.name, glyph, detail)
    console.print(t)
    summary = (
        f"[{OK}]{counts['ok']} ok[/{OK}]  "
        f"[{WARN}]{counts['warn']} warn[/{WARN}]  "
        f"[{ERR}]{counts['fail']} fail[/{ERR}]"
    )
    console.print(summary)
    if counts["fail"]:
        console.print(
            f"[{DIM}]fix the failing checks above before running `autosentry run` "
            f"(or pass --fix to auto-repair safe ones).[/{DIM}]"
        )
    elif counts["warn"]:
        console.print(
            f"[{DIM}]warnings don't block operation but may degrade features. "
            f"Pass --fix to auto-repair safe ones.[/{DIM}]"
        )
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


# pyrefly suppression: field is imported above for future Repair extensions.
_ = field
_ = Any
