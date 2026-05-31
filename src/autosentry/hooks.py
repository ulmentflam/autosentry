"""Stop-hook installer for the Claude Code session-dispatch flow.

Under ``dispatch.mode: session`` (see :class:`autosentry.config.DispatchConfig`),
the interactive Claude Code session is the dispatcher of last resort. To
keep watching across user turns we install a Claude Code **Stop hook**
that runs ``autosentry probe --inject-prompt --quiet`` after every
assistant turn. When the probe finds pending incidents or sees that the
autosentry monitor is down / stale, its JSON output re-engages the
session via ``decision: block`` so the model can dispatch healers.

This module owns the read-modify-write of ``.claude/settings.local.json``
(creating it if missing). The hook entry is **idempotent**: re-running
``autosentry init`` or ``autosentry hooks install`` won't duplicate it.
The match key is the ``command`` string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

#: The command the Stop hook runs. Stable; bumping this string is what
#: makes the idempotency check work — never edit it casually.
STOP_HOOK_COMMAND = "autosentry probe --inject-prompt --quiet"

#: Where Claude Code reads per-project hook settings. Local (not
#: ``settings.json``) so a checked-in repo config never overrides a
#: user's own ``.claude/`` choices.
CLAUDE_SETTINGS_REL = Path(".claude/settings.local.json")


@dataclass(frozen=True)
class HookInstallResult:
    settings_path: Path
    status: Literal["created", "added", "already_present", "skipped"]


def install_claude_stop_hook(
    target_dir: Path,
    *,
    settings_rel: Path = CLAUDE_SETTINGS_REL,
    command: str = STOP_HOOK_COMMAND,
) -> HookInstallResult:
    """Ensure the Claude Code Stop hook is present in ``settings.local.json``.

    Idempotent — running this twice leaves exactly one matching hook
    entry. Preserves any other hooks the user has already configured.
    """
    settings_path = target_dir / settings_rel
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any]
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            # Don't clobber a partially-written file the operator may
            # still be editing — surface as ``skipped`` and bail.
            return HookInstallResult(settings_path=settings_path, status="skipped")
        existed = True
    else:
        data = {}
        existed = False

    hooks = data.setdefault("hooks", {})
    stop_groups = hooks.setdefault("Stop", [])

    # Each "group" carries an optional matcher and a list of commands.
    # An exact command match in any group counts as already-present.
    if _has_command(stop_groups, command):
        if not existed:
            settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return HookInstallResult(settings_path=settings_path, status="created")
        return HookInstallResult(settings_path=settings_path, status="already_present")

    stop_groups.append({"hooks": [{"type": "command", "command": command}]})
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return HookInstallResult(
        settings_path=settings_path,
        status="created" if not existed else "added",
    )


def _has_command(stop_groups: list[Any], command: str) -> bool:
    for group in stop_groups:
        if not isinstance(group, dict):
            continue
        for h in group.get("hooks", []) or []:
            if isinstance(h, dict) and h.get("command") == command:
                return True
    return False


def uninstall_claude_stop_hook(
    target_dir: Path,
    *,
    settings_rel: Path = CLAUDE_SETTINGS_REL,
    command: str = STOP_HOOK_COMMAND,
) -> bool:
    """Remove our Stop-hook entry from ``settings.local.json``. Returns
    True if an entry was removed. Leaves the file (and any other hooks)
    in place; deletes empty hook structures we created so we don't
    leave orphaned empty arrays behind.
    """
    settings_path = target_dir / settings_rel
    if not settings_path.exists():
        return False
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError:
        return False
    hooks = data.get("hooks", {})
    stop_groups = hooks.get("Stop", [])
    if not stop_groups:
        return False

    removed = False
    new_groups: list[Any] = []
    for group in stop_groups:
        if not isinstance(group, dict):
            new_groups.append(group)
            continue
        kept = [
            h
            for h in group.get("hooks", []) or []
            if not (isinstance(h, dict) and h.get("command") == command)
        ]
        if len(kept) != len(group.get("hooks", []) or []):
            removed = True
        if kept:
            group["hooks"] = kept
            new_groups.append(group)
        # else: drop the empty group entirely

    if not removed:
        return False

    if new_groups:
        hooks["Stop"] = new_groups
    else:
        hooks.pop("Stop", None)
    if not hooks:
        data.pop("hooks", None)
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True
