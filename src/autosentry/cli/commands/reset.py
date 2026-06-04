"""``autosentry reset`` — clear the restart budget without losing audit history.

When the unverified-restart counter has burned through (the monitor
parked itself with "max restarts reached"), the right escape hatch is
to clear the counter, leave the audit trail, and let the supervisor
try again. This subcommand does that:

1. Stop the supervised child if its pid in state.json is still alive.
2. Zero ``state.restarts`` (and ``last_recovery_failed_for``); keep
   ``restarts_total`` and ``restart_history`` so the prior failures
   stay readable from `autosentry status` and the vault.
3. Append a ``ResetRecord`` to state.json and mirror a one-line entry
   to ``.autosentry/reset.log`` so ops can ``tail -F`` the reasons.
4. Optionally relaunch the supervisor (default). Pass ``--no-restart``
   to clear-and-exit (useful in scripts that want to inspect state
   before the next run, or when the next run is a different stage).

The plain-text log file is append-only and never rotated by autosentry
itself — its size is the count of resets, which should be tiny.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import typer

from autosentry.cli import app
from autosentry.cli.style import DIM, INFO, OK, WARN, confirm, console, is_interactive
from autosentry.config import DEFAULT_CONFIG_PATH, load_config
from autosentry.state import StateStore, append_reset_log


@app.command()
def reset(
    config: Path = typer.Option(  # noqa: B008
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help="Path to the config. Defaults to .autosentry/autosentry.yaml.",
    ),
    reason: str = typer.Option(
        "manual reset",
        "--reason",
        "-m",
        help="One-line note recorded in state.json's reset_history and reset.log.",
    ),
    no_restart: bool = typer.Option(
        False,
        "--no-restart",
        help="Clear counters but don't relaunch the supervisor.",
    ),
    full: bool = typer.Option(
        False,
        "--full",
        help=(
            "Also drop restart_history (audit log of prior restarts). "
            "Requires --force. Use sparingly — vault/web lose their "
            "in-state history of this monitor."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Skip the confirmation prompt (and required for --full).",
    ),
) -> None:
    """Clear the restart budget; optionally relaunch the supervisor.

    Default flow: stop the running child (if any), zero
    ``state.restarts``, log a ``ResetRecord``, then run
    ``autosentry run`` in the foreground. Pass ``--no-restart`` to
    skip the relaunch.

    The audit trail is the point of this command — anyone reading
    `autosentry status` afterward should be able to see *that* a
    reset happened, *when*, and *why*. ``--full`` is the only mode
    that drops history; it is gated behind ``--force`` so the wipe
    is always intentional.

    Use this when:

    - The monitor has parked itself with "max restarts reached" and
      you've fixed the underlying problem out-of-band.
    - You're starting a fresh experiment in the same repo and want a
      clean budget for the new run.
    - You want to test the healer flow from a known-zero state.
    """
    if full and not force:
        console.print(
            f"[{WARN}]--full drops restart_history (audit trail). Pass --force to confirm.[/{WARN}]"
        )
        raise typer.Exit(code=2)

    cfg = load_config(config)
    state_path = cfg.resolve(cfg.state_path)
    if not state_path.exists():
        console.print(f"[{WARN}]no state.json at {state_path} — nothing to reset.[/{WARN}]")
        raise typer.Exit(code=1)

    store = StateStore(state_path)
    state = store.load()

    if not force and is_interactive():
        scope = "restart counters" + (" + restart_history" if full else "")
        if state.pid:
            scope = f"stop pid {state.pid} and clear {scope}"
        if not confirm(f"Reset will {scope}. Continue?", default=True):
            console.print(f"[{DIM}](aborted){DIM}")
            raise typer.Exit(code=1)

    # Step 1 — stop the running child if state.json claims one.
    stopped_pid = _maybe_stop_running_pid(state.pid)
    if stopped_pid is not None:
        console.print(f"  [{OK}]✓[/{OK}] stopped pid {stopped_pid}")
        state.pid = None

    # Step 2 — capture counters, then zero them. Mirror to reset.log.
    record = state.record_reset(reason=reason, clear_history=full, source="manual")
    store.save(state)
    reset_log = cfg.resolve(cfg.monitor.log_dir) / "reset.log"
    append_reset_log(reset_log, record)
    console.print(
        f"  [{OK}]✓[/{OK}] cleared restarts "
        f"[{DIM}](was {record.prior_restarts}, total={record.prior_restarts_total} kept)[/{DIM}]"
    )
    console.print(f"  [{INFO}]·[/{INFO}] logged → {reset_log}")

    # Step 3 — optionally relaunch. We exec a fresh `autosentry run`
    # rather than calling Monitor.run() in-process so signals + logging
    # behave the same as any normal startup.
    if no_restart:
        console.print(f"  [{DIM}](--no-restart — supervisor not relaunched){DIM}")
        return

    console.print(f"\n[{INFO}]relaunching supervisor…[/{INFO}]")
    cmd = [sys.executable, "-m", "autosentry", "run", "--config", str(config)]
    # Use exec so the new process replaces this CLI invocation — signal
    # forwarding is then just whatever the user sent (SIGINT → child).
    try:
        os.execvp(cmd[0], cmd)  # noqa: S606 — argv is built from sys.executable + literals
    except OSError as e:
        # Fall back to spawn if exec fails (rare; mostly /proc-less envs).
        console.print(f"[{WARN}]exec failed ({e}); spawning child instead[/{WARN}]")
        subprocess.run(cmd, check=False)  # noqa: S603


def _maybe_stop_running_pid(pid: int | None, *, grace_seconds: float = 10.0) -> int | None:
    """SIGTERM the process if alive; return the pid actually stopped.

    Returns None if there was no pid, the pid was already gone, or the
    pid belongs to something other than the supervisor (we never SIGKILL
    blindly — better to leave a stranger alone than to terminate an
    unrelated process that happens to have recycled the pid).
    """
    if pid is None or pid <= 0:
        return None
    if not _pid_alive(pid):
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    except PermissionError:
        # Not ours — refuse to escalate.
        return None
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return pid
        time.sleep(0.2)
    # SIGTERM didn't take. Don't SIGKILL — surface that we couldn't
    # confirm it stopped. The state is still cleared; the user can
    # investigate the orphan separately.
    return pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
