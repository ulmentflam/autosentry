"""``autosentry healer`` subcommands.

Today there's exactly one — ``respond`` — which a subagent runs to
produce ``.autosentry/recovery_response.md`` after diagnosing an
incident in interactive mode. Future commands (e.g. ``request --inspect``,
``request --inject``) can land here too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import typer

from autosentry.cli import app
from autosentry.cli.style import ERR, OK, console
from autosentry.config import DEFAULT_CONFIG_PATH, load_config

healer_app = typer.Typer(
    no_args_is_help=True,
    help="Interact with the Claude healer's request/response files.",
)
app.add_typer(healer_app, name="healer")

# Mirrors the RuleAction.kind enum exactly so the parser the healer uses
# round-trips cleanly. Keep in sync if you add a new action kind.
_VALID_ACTIONS = ("restart", "restart_with_env", "pause", "abort", "custom_command")


@healer_app.command("respond")
def respond(
    config: Path = typer.Option(  # noqa: B008
        DEFAULT_CONFIG_PATH, "--config", "-c"
    ),
    action: str = typer.Option(
        "restart",
        "--action",
        help=f"Action kind. Choices: {', '.join(_VALID_ACTIONS)}",
    ),
    set_pairs: list[str] = typer.Option(  # noqa: B008
        [],
        "--set",
        "-s",
        help=(
            "KEY=VALUE pair for restart_with_env actions. Repeat for "
            "multiple. Ignored for other action kinds."
        ),
    ),
    diagnosis: str = typer.Option(
        "",
        "--diagnosis",
        "-d",
        help=(
            "Free-form text appended after the ACTION block. Lands in the "
            "incident folder as claude_response.md so an operator can "
            "review the subagent's reasoning."
        ),
    ),
    incident_id: str = typer.Option(
        "",
        "--incident-id",
        help=(
            "Incident the response belongs to. Currently informational; "
            "the healer looks up the most recent pending fix by detector. "
            "Defaults to the value the request file declared."
        ),
    ),
) -> None:
    """Produce the recovery_response.md file the interactive healer is
    blocked waiting on.

    Typical caller: a subagent the /autosentry skill spawned to
    diagnose a recovery request. The subagent decides on an action
    (restart / restart_with_env / pause / abort / custom_command),
    captures any env-var changes via --set, and writes a one-line
    --diagnosis. autosentry's healer reads the response within the
    next tick (~1s) and applies the action.

    --set is ignored for action kinds other than restart_with_env.
    Atomic write (tmp + rename) so the healer's mtime check sees a
    complete file. Run this once per spawn; the response file is
    overwritten if it exists.

    Example:

        autosentry healer respond \\
          --action restart_with_env \\
          --set BATCH_SIZE=4 \\
          --diagnosis "OOM at step 8450; halving batch."
    """
    if action not in _VALID_ACTIONS:
        console.print(
            f"[{ERR}]invalid --action '{action}'[/{ERR}] — choose one of: "
            f"{', '.join(_VALID_ACTIONS)}"
        )
        raise typer.Exit(code=2)

    cfg = load_config(config)
    response_path = cfg.resolve(cfg.healing.claude.response_path)
    response_path.parent.mkdir(parents=True, exist_ok=True)
    body = _format_response(
        action=_as_action_literal(action),
        set_pairs=set_pairs,
        diagnosis=diagnosis,
        incident_id=incident_id,
    )
    # Write to a tmp file + rename so the healer's mtime check sees a
    # complete file (atomicity matters for the file-handshake).
    tmp = response_path.with_suffix(response_path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(response_path)
    console.print(f"[{OK}]✓[/{OK}] wrote {response_path}")


def _as_action_literal(
    value: str,
) -> Literal["restart", "restart_with_env", "pause", "abort", "custom_command"]:
    # Already validated by the caller; this is just to satisfy the type
    # checker without an explicit cast import at the top of the file.
    return value  # type: ignore[return-value]


def _format_response(
    *,
    action: Literal["restart", "restart_with_env", "pause", "abort", "custom_command"],
    set_pairs: list[str],
    diagnosis: str,
    incident_id: str,
) -> str:
    lines: list[str] = []
    if incident_id:
        lines.append(f"<!-- incident_id: {incident_id} -->")
    lines.append(f"ACTION: {action}")
    if action == "restart_with_env":
        for pair in set_pairs:
            if "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            lines.append(f"SET: {key.strip()}={value.strip()}")
    if diagnosis:
        lines.append("")
        lines.append(diagnosis.rstrip())
    lines.append("")
    return "\n".join(lines)
