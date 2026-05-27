"""``autosentry onboard`` — phase-aware setup walkthrough.

Designed for both humans (rich panels, hints, prompts) and AI agents
(non-TTY → plain bullets, structured output). Detects which phase the
user is in (uninstalled / not initialized / not running / running) and
prints the right next step.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import typer

from autosentry import __version__
from autosentry.cli import app
from autosentry.cli.style import (
    ACCENT,
    DIM,
    INFO,
    OK,
    WARN,
    banner,
    console,
    is_interactive,
)


@app.command()
def onboard(
    config: Path = typer.Option(  # noqa: B008
        Path("autosentry.yaml"), "--config", "-c"
    ),
    for_agent: bool = typer.Option(
        False,
        "--for-agent",
        help=(
            "Print a structured AI-agent playbook (plain bullets, no rich panels). "
            "Auto-enabled when stdout is not a TTY."
        ),
    ),
) -> None:
    """Phase-aware setup playbook for a repo.

    Detects which step the repo is at — no autosentry.yaml,
    configured but not running, or running — and prints the right
    next-step commands. For a real terminal you get rich panels;
    when stdout is piped or you pass --for-agent, output drops to
    plain bullets that an AI tool can parse from a Bash tool result.
    Use this from inside Claude Code / Cursor / etc. when stepping
    into an unfamiliar repo.
    """
    plain = for_agent or not is_interactive()
    phase = _detect_phase(config)
    if plain:
        _print_agent(phase, config)
    else:
        _print_human(phase, config)


# ----- phase detection -----------------------------------------------------


def _detect_phase(config: Path) -> str:
    """Return one of: ``uninstalled``, ``no_config``, ``not_running``, ``running``."""
    # If we're running this command at all, the CLI is installed. We keep
    # ``uninstalled`` in the enum for the playbook docs in case someone
    # follows it from another machine.
    cwd = Path.cwd()
    if not (cwd / config).exists():
        return "no_config"
    state = cwd / ".autosentry" / "state.json"
    if not state.exists():
        return "not_running"
    if _pid_alive_from_state(state):
        return "running"
    return "not_running"


def _pid_alive_from_state(state_path: Path) -> bool:
    try:
        import json as _json

        data = _json.loads(state_path.read_text(encoding="utf-8"))
        pid = data.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        import os as _os

        _os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


# ----- human-friendly output -----------------------------------------------


def _print_human(phase: str, config: Path) -> None:
    console.print(banner(__version__, tagline="onboarding"))
    console.print(f"[{DIM}]phase: [/{DIM}][{INFO}]{phase}[/{INFO}]\n")

    if phase == "no_config":
        console.print(f"[{ACCENT}]no autosentry.yaml here yet[/{ACCENT}]")
        console.print(
            f"  [{OK}]1.[/{OK}] [bold]autosentry init[/bold]   "
            f"[{DIM}]# scaffolds autosentry.yaml + .autosentry/[/{DIM}]"
        )
        console.print(
            f"  [{OK}]2.[/{OK}] [bold]autosentry doctor[/bold] "
            f"[{DIM}]# confirm env is healthy[/{DIM}]"
        )
        console.print(
            f"  [{OK}]3.[/{OK}] Edit [bold]autosentry.yaml[/bold] — set process.command + detectors"
        )
        console.print(f"  [{OK}]4.[/{OK}] [bold]autosentry run[/bold]")
        _print_skills_hint()
        return

    if phase == "not_running":
        console.print(f"[{ACCENT}]configured but not running[/{ACCENT}]")
        console.print(
            f"  [{OK}]1.[/{OK}] [bold]autosentry doctor[/bold] "
            f"[{DIM}]# confirm env is healthy[/{DIM}]"
        )
        console.print(f"  [{OK}]2.[/{OK}] Foreground (first run): [bold]autosentry run[/bold]")
        console.print(
            f"         [{DIM}]or background: nohup autosentry run > /dev/null 2>&1 &[/{DIM}]"
        )
        console.print(
            f"  [{OK}]3.[/{OK}] Tail the structured log: "
            f"[bold]tail -F .autosentry/logs/autosentry.log[/bold]"
        )
        console.print(f"  [{OK}]4.[/{OK}] Live dashboard: [bold]autosentry watch[/bold]")
        _print_skills_hint()
        return

    if phase == "running":
        console.print(f"[{ACCENT}]monitor is running — operating mode[/{ACCENT}]")
        console.print(f"  [{OK}]·[/{OK}] [bold]autosentry status[/bold] — pid, restarts, anomalies")
        console.print(f"  [{OK}]·[/{OK}] [bold]autosentry watch[/bold] — live TUI")
        console.print(f"  [{OK}]·[/{OK}] [bold]autosentry web[/bold] — browse incidents")
        console.print(f"  [{OK}]·[/{OK}] [bold]autosentry incidents list[/bold]")
        console.print(f"  [{OK}]·[/{OK}] [bold]autosentry analyze --since 24h[/bold]")
        console.print(
            f"  [{DIM}]If three+ incidents for the same detector all `kept` via Claude, "
            f"codify a YAML rule.[/{DIM}]"
        )
        return

    console.print(
        f"[{WARN}]autosentry CLI isn't on PATH — "
        f"install with the one-liner from the README.[/{WARN}]"
    )


def _print_skills_hint() -> None:
    console.print(f"\n[{DIM}]tip: drop a /autosentry slash command into your AI editor:[/{DIM}]")
    console.print("  [bold]autosentry skills install --tool all[/bold]")


# ----- agent-readable output (plain bullets) -------------------------------


def _print_agent(phase: str, config: Path) -> None:
    """Plain text, no ANSI, easy for an LLM to parse from a tool result."""
    print(f"phase: {phase}")
    print(f"autosentry: {__version__}")
    print(f"config: {config}")

    if phase == "no_config":
        print("next:")
        print("  - autosentry init")
        print("  - autosentry doctor")
        print("  - edit autosentry.yaml: set process.command + detectors + rules")
        print("  - autosentry run")
        return
    if phase == "not_running":
        print("next:")
        print("  - autosentry doctor")
        print("  - nohup autosentry run > /dev/null 2>&1 &")
        print("  - tail -F .autosentry/logs/autosentry.log")
        print("  - autosentry watch")
        return
    if phase == "running":
        print("operate:")
        print("  - autosentry status")
        print("  - autosentry watch")
        print("  - autosentry web")
        print("  - autosentry incidents list")
        print("  - autosentry analyze --since 24h")
        print("rule:")
        print(
            "  - if 3+ kept Claude fixes for the same detector, "
            "codify a YAML rule in autosentry.yaml"
        )
        return
    print("next:")
    print(
        "  - install: curl -fsSL "
        "https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh"
    )


# ----- helpers (subprocess) - mostly unused; kept for parity with doctor ---


def _which(name: str) -> str | None:
    return shutil.which(name)


def _git_status_text(cwd: Path) -> str:
    if _which("git") is None:
        return ""
    r = subprocess.run(  # noqa: S603
        ["git", "-C", str(cwd), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return r.stdout if r.returncode == 0 else ""
