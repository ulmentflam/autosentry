"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from autosentry.config import AutoSentryConfig, load_config


@pytest.fixture
def minimal_config(tmp_path: Path) -> AutoSentryConfig:
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            """\
            process:
              kind: local
              command: ["true"]
              cwd: "."
              env: {}
              restart_policy:
                max_restarts: 2
                cooldown_seconds: 0
            monitor:
              poll_interval_seconds: 1
              log_dir: ".autosentry/logs"
            detectors:
              - kind: pattern
                name: oom
                regex: "OutOfMemoryError"
              - kind: traceback
              - kind: exit_code
            rules:
              - match: { detector: oom }
                action: { kind: restart, notify: true }
            state_path: ".autosentry/state.json"
            incidents_dir: ".autosentry/incidents"
            """
        )
    )
    return load_config(cfg_path)
