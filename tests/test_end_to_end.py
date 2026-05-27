"""End-to-end smoke: launch a failing subprocess, expect an incident folder.

Uses a tiny Python script that prints a traceback and exits non-zero. Verifies
the supervisor + traceback detector + incident store wire up correctly.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from textwrap import dedent

from autosentry.config import load_config
from autosentry.monitor import Monitor


def test_failing_subprocess_produces_incident(tmp_path: Path, monkeypatch):
    # Failing script
    script = tmp_path / "failing.py"
    script.write_text(
        dedent(
            """\
            import sys
            def step():
                raise ValueError("boom")
            def main():
                step()
            if __name__ == "__main__":
                main()
            """
        )
    )

    # Config: run the failing script with python; expect a traceback detection
    # and a max_restarts of 1 so the monitor stops promptly.
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            f"""\
            process:
              kind: local
              command: ["{sys.executable}", "{script}"]
              restart_policy:
                max_restarts: 0
                cooldown_seconds: 0
            monitor:
              poll_interval_seconds: 1
              log_dir: ".autosentry/logs"
            detectors:
              - kind: traceback
              - kind: exit_code
            rules: []
            healing:
              claude:
                enabled: false
            state_path: ".autosentry/state.json"
            incidents_dir: ".autosentry/incidents"
            """
        )
    )

    cfg = load_config(cfg_path)
    monitor = Monitor(cfg)

    # Run in a background thread so the test can timebox itself.
    import threading

    t = threading.Thread(target=monitor.run, daemon=True)
    t.start()
    deadline = time.time() + 20
    incidents_dir = cfg.resolve(cfg.incidents_dir)
    found = False
    while time.time() < deadline:
        if incidents_dir.exists() and any(incidents_dir.iterdir()):
            found = True
            break
        time.sleep(0.2)

    # Stop the monitor regardless of outcome.
    monitor._stop = True
    t.join(timeout=5)

    assert found, f"expected an incident folder under {incidents_dir}"
    incidents = [p for p in incidents_dir.iterdir() if p.is_dir()]
    assert incidents, "no incident folders written"
    # At least one of them should be a traceback or exit-code incident with a report.
    has_report = any((p / "report.md").exists() for p in incidents)
    assert has_report
