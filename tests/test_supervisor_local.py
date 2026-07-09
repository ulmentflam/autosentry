"""Local supervisor tests.

Focus on the log-stream lifecycle across a restart — the root cause of
issue #9, where the monitor's single ``iter_log_lines`` iterator used to
terminate on the *first* child's end-of-stream sentinel and never see the
restarted child's output.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from textwrap import dedent

from autosentry.config import RuleAction, load_config
from autosentry.supervisors import supervisor_for
from autosentry.supervisors.base import LogLine
from autosentry.supervisors.local import LocalSupervisor


def _local_supervisor(tmp_path: Path) -> LocalSupervisor:
    # A short-lived child that echoes its PHASE env var a handful of times
    # then exits, so we can distinguish the pre- and post-restart children
    # purely from the log stream.
    script = tmp_path / "child.py"
    script.write_text(
        dedent(
            """\
            import os, time
            phase = os.environ.get("PHASE", "A")
            for i in range(5):
                print(f"{phase}-{i}", flush=True)
                time.sleep(0.02)
            """
        )
    )
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            f"""\
            process:
              kind: local
              command: [{sys.executable!r}, {str(script)!r}]
            monitor:
              log_dir: "logs"
            """
        )
    )
    cfg = load_config(cfg_path)
    sup = supervisor_for(cfg, log_dir=tmp_path / "logs")
    assert isinstance(sup, LocalSupervisor)
    return sup


def _drain_until(it, predicate, *, timeout: float = 5.0) -> list[str]:
    """Pull lines from the iterator until ``predicate(collected)`` or timeout."""
    collected: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = next(it)
        if isinstance(item, LogLine):
            collected.append(item.text)
            if predicate(collected):
                return collected
    return collected


def test_iter_log_lines_survives_restart(tmp_path: Path):
    """Regression for issue #9: after a restart, the same iterator must keep
    delivering the *new* child's lines rather than terminating on the old
    child's end-of-stream sentinel."""
    sup = _local_supervisor(tmp_path)
    try:
        sup.start()
        it = sup.iter_log_lines()

        # First child: PHASE defaults to "A".
        first = _drain_until(it, lambda c: any(x.startswith("A-") for x in c))
        assert any(x.startswith("A-") for x in first), first

        # Restart with a new PHASE so the replacement child's lines are
        # distinguishable. This tears down the old child (its reader thread
        # posts the sentinel) and spawns a fresh one.
        sup.apply_action(RuleAction(kind="restart_with_env", set={"PHASE": "B"}))

        # The SAME iterator must now yield the restarted child's "B-" lines.
        # Before the fix it stopped at the first child's sentinel and this
        # drain would time out with nothing.
        second = _drain_until(it, lambda c: any(x.startswith("B-") for x in c))
        assert any(x.startswith("B-") for x in second), second
    finally:
        sup.stop()


def test_started_at_changes_on_restart(tmp_path: Path):
    """The monitor keys its detector-reset on ``started_at`` changing, so a
    restart must produce a fresh stamp."""
    sup = _local_supervisor(tmp_path)
    try:
        sup.start()
        first_stamp = sup.status().started_at
        assert first_stamp is not None
        sup._restart()
        second_stamp = sup.status().started_at
        assert second_stamp is not None
        assert second_stamp != first_stamp
    finally:
        sup.stop()
