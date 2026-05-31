"""``autosentry session`` — session-dispatch helpers.

Used by the ``/autosentry`` skill when the monitor is in
``dispatch.mode: session``. The session reads pending incidents via
``autosentry probe`` and then asks the monitor to apply a fix via
``autosentry session apply``.

This command does NOT execute the action itself — it appends a JSON
line to ``.autosentry/session_actions.jsonl`` (the queue
:meth:`autosentry.monitor.Monitor._drain_session_action_queue` reads on
each tick). Keeps process-management invariants inside the monitor
where they belong; the session is the *decider*, the monitor is the
*doer*.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import typer

from autosentry.cli import app
from autosentry.cli.style import DIM, OK, console
from autosentry.config import DEFAULT_CONFIG_PATH, load_config

session_app = typer.Typer(
    name="session",
    help="Session-dispatch helpers used by the /autosentry skill.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(session_app, name="session")


_ACTION_KINDS = ("restart", "restart_with_env", "pause", "abort", "custom_command")


@session_app.command("apply")
def apply(
    config: Path = typer.Option(  # noqa: B008
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help="Path to the config (default .autosentry/autosentry.yaml).",
    ),
    incident: str = typer.Option(
        ...,
        "--incident",
        help="Incident id this action resolves (folder name under .autosentry/incidents/).",
    ),
    action: str = typer.Option(
        ...,
        "--action",
        help=f"Action kind. One of: {', '.join(_ACTION_KINDS)}.",
    ),
    set_pairs: list[str] = typer.Option(  # noqa: B008
        [],
        "--set",
        help="KEY=VALUE env override for restart_with_env. Repeat for multiple.",
    ),
    command: list[str] = typer.Option(  # noqa: B008
        [],
        "--command",
        help="Command argv for custom_command action. Repeat for each arg.",
    ),
    rule: str | None = typer.Option(
        None,
        "--rule",
        help="Optional rule name to attribute this action to (informational; recorded in state).",
    ),
    source: str = typer.Option(
        "manual",
        "--source",
        help="Where the dispatch decision came from — 'rule', 'claude', or 'manual'.",
    ),
    no_notify: bool = typer.Option(
        False,
        "--no-notify",
        help="Set action.notify=false so the monitor doesn't fire a notifier on apply.",
    ),
) -> None:
    """Ask the monitor to apply ``action`` for ``incident``.

    Appends a JSON line to the session action queue. The monitor drains
    the queue on its next tick (≤ poll_interval_seconds) and applies
    via ``supervisor.apply_action``. Failures are logged to
    ``.autosentry/logs/autosentry.log``; this command exits 0 once the
    line is written.
    """
    if action not in _ACTION_KINDS:
        raise typer.BadParameter(f"--action must be one of: {', '.join(_ACTION_KINDS)}")
    if action == "restart_with_env" and not set_pairs:
        raise typer.BadParameter("restart_with_env requires at least one --set KEY=VALUE")
    if action == "custom_command" and not command:
        raise typer.BadParameter("custom_command requires --command argv")

    set_map: dict[str, str] = {}
    for pair in set_pairs:
        if "=" not in pair:
            raise typer.BadParameter(f"--set expects KEY=VALUE, got {pair!r}")
        k, v = pair.split("=", 1)
        set_map[k] = v

    cfg = load_config(config)
    queue_path = cfg.resolve(cfg.dispatch.action_queue_path)
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    # Monotonic-ish id: timestamp + short uuid suffix. Lexicographic
    # sort matches insertion order even across processes.
    entry_id = f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"
    payload: dict[str, Any] = {
        "id": entry_id,
        "incident_id": incident,
        "rule": rule,
        "source": source,
        "action": {
            "kind": action,
            "set": set_map,
            "command": list(command),
            "notify": not no_notify,
        },
    }
    with queue_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")

    console.print(
        f"[{OK}]✓[/{OK}] session action queued "
        f"[{DIM}](id={entry_id} incident={incident} action={action})[/{DIM}]"
    )
