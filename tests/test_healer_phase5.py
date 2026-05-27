"""Phase 5 — interactive ClaudeHealer + healer respond CLI.

Tests the mode-resolution matrix, the file handshake, the subagent
pick from config, and the new ``autosentry healer respond`` CLI.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from ruamel.yaml import YAML
from typer.testing import CliRunner

from autosentry.cli import app
from autosentry.config import SubagentSpec, load_config
from autosentry.detectors.base import Detection
from autosentry.healers.claude import ClaudeHealer, _format_request, _parse_action

_yaml = YAML(typ="safe")
_yaml.default_flow_style = False

# ----- fixtures -----------------------------------------------------------


def _write_cfg(tmp_path: Path, **claude_overrides: Any) -> Path:
    """Write a minimal autosentry.yaml with ``healing.claude`` overrides
    merged in. We build the dict and dump it via PyYAML so we avoid the
    ruamel duplicate-key trap that string templating hit."""
    cfg = tmp_path / "autosentry.yaml"
    claude_block: dict[str, Any] = {
        "enabled": "auto",
        "mode": "auto",
        "timeout_seconds": 5,
    }
    claude_block.update(claude_overrides)
    data = {
        "process": {"kind": "local", "command": ["true"]},
        "healing": {"claude": claude_block},
        "state_path": ".autosentry/state.json",
        "incidents_dir": ".autosentry/incidents",
    }
    with cfg.open("w", encoding="utf-8") as f:
        _yaml.dump(data, f)
    return cfg


# ----- mode resolution ----------------------------------------------------


def test_mode_resolution_skill_present_wins(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path))
    # Drop an AGENTS.md to make the skill marker present.
    (tmp_path / "AGENTS.md").write_text("# agents")
    healer = ClaudeHealer(cfg)
    assert healer._resolve_mode() == "interactive"


def test_mode_resolution_falls_back_to_subprocess_when_only_cli(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path))
    # No skill markers; pretend `claude` is on PATH.
    with patch("autosentry.healers.claude.shutil.which", return_value="/usr/bin/claude"):
        healer = ClaudeHealer(cfg)
        assert healer._resolve_mode() == "subprocess"


def test_mode_resolution_disabled_when_neither(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path))
    with patch("autosentry.healers.claude.shutil.which", return_value=None):
        healer = ClaudeHealer(cfg)
        assert healer._resolve_mode() == "disabled"


def test_mode_resolution_explicit_interactive_overrides(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path, mode="interactive"))
    # Even without skill or CLI, explicit interactive stays interactive.
    with patch("autosentry.healers.claude.shutil.which", return_value=None):
        healer = ClaudeHealer(cfg)
        assert healer._resolve_mode() == "interactive"


def test_disabled_short_circuits(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path, enabled=False))
    healer = ClaudeHealer(cfg)
    out = healer.attempt(
        Detection(detector="x", kind="error", message="hi"),
        state_dict={},
        last_incident_dir=None,
    )
    assert out is None


# ----- subagent picking ---------------------------------------------------


def test_subagent_lookup_uses_detector_specific(tmp_path: Path):
    cfg = load_config(
        _write_cfg(
            tmp_path,
            subagents={
                "default": {"type": "general-purpose", "description": "default"},
                "training_stall": {"type": "Plan", "description": "stall-specific"},
            },
        )
    )
    healer = ClaudeHealer(cfg)
    spec = healer._pick_subagent(
        Detection(detector="training_stall", kind="anomaly", message="stalled")
    )
    assert spec.type == "Plan"
    assert "stall" in spec.description.lower()


def test_subagent_lookup_falls_back_to_default(tmp_path: Path):
    cfg = load_config(
        _write_cfg(
            tmp_path,
            subagents={"default": {"type": "Explore", "description": "default"}},
        )
    )
    healer = ClaudeHealer(cfg)
    spec = healer._pick_subagent(Detection(detector="oom", kind="error", message="OOM"))
    assert spec.type == "Explore"


def test_subagent_lookup_uses_builtin_when_no_config(tmp_path: Path):
    cfg = load_config(_write_cfg(tmp_path))
    healer = ClaudeHealer(cfg)
    spec = healer._pick_subagent(Detection(detector="x", kind="error", message="x"))
    assert spec.type == "general-purpose"


# ----- request file format -----------------------------------------------


def test_format_request_has_frontmatter_and_body():
    body = _format_request(
        incident_id="INC-1",
        detector="training_stall",
        subagent=SubagentSpec(type="Plan", description="diagnose stall"),
        timeout_seconds=42,
        prompt="DIAGNOSE THIS",
    )
    assert body.startswith("---\n")
    assert "incident_id: INC-1" in body
    assert "detector: training_stall" in body
    assert "type: Plan" in body
    assert "timeout_seconds: 42" in body
    assert "DIAGNOSE THIS" in body
    # Body comes after the second `---` fence.
    fence_indices = [i for i, line in enumerate(body.splitlines()) if line == "---"]
    assert len(fence_indices) >= 2


# ----- interactive file handshake ----------------------------------------


def test_interactive_attempt_blocks_then_reads_response(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path, mode="interactive"))
    healer = ClaudeHealer(cfg)

    # Spawn a writer thread that drops a response file shortly after the
    # healer starts blocking.
    response_path = cfg.resolve(cfg.healing.claude.response_path)

    def write_response_after_delay():
        time.sleep(0.2)
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_text("ACTION: restart_with_env\nSET: FOO=bar\n")

    t = threading.Thread(target=write_response_after_delay, daemon=True)
    t.start()

    out = healer.attempt(
        Detection(detector="x", kind="error", message="hi"),
        state_dict={"pid": 1},
        last_incident_dir=None,
    )
    t.join(timeout=2)
    assert out is not None
    assert out.source == "claude"
    assert out.action is not None
    assert out.action.kind == "restart_with_env"
    assert out.action.set == {"FOO": "bar"}
    # Request file should have been written too.
    req = cfg.resolve(cfg.healing.claude.request_path)
    assert req.exists()
    assert "incident_id" in req.read_text()


def test_interactive_attempt_times_out_returns_none(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path, mode="interactive", timeout_seconds=1))
    healer = ClaudeHealer(cfg)
    # No writer thread → must time out.
    out = healer.attempt(
        Detection(detector="x", kind="error", message="hi"),
        state_dict={},
        last_incident_dir=None,
    )
    assert out is None


def test_interactive_ignores_stale_response(tmp_path: Path, monkeypatch):
    """Pre-existing response file with old mtime must not be consumed."""
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_write_cfg(tmp_path, mode="interactive", timeout_seconds=1))
    response_path = cfg.resolve(cfg.healing.claude.response_path)
    response_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_text("ACTION: restart\n")
    # Make sure its mtime is in the past so the healer's baseline freezes it.
    old = time.time() - 60
    import os as _os

    _os.utime(response_path, (old, old))

    healer = ClaudeHealer(cfg)
    out = healer.attempt(
        Detection(detector="x", kind="error", message="hi"),
        state_dict={},
        last_incident_dir=None,
    )
    # Stale response → no fresh response within timeout → None.
    assert out is None


# ----- response parse round-trip -----------------------------------------


def test_parse_action_round_trip():
    action = _parse_action("ACTION: restart_with_env\nSET: BATCH_SIZE=4\nSET: NUM_WORKERS=2\n")
    assert action.kind == "restart_with_env"
    assert action.set == {"BATCH_SIZE": "4", "NUM_WORKERS": "2"}


def test_parse_action_defaults_to_restart_when_missing():
    action = _parse_action("no action block here, just prose")
    assert action.kind == "restart"


def test_parse_action_invalid_kind_falls_back_to_restart():
    action = _parse_action("ACTION: take_over_the_world\n")
    assert action.kind == "restart"


# ----- healer respond CLI ------------------------------------------------


def test_healer_respond_writes_expected_file(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_cfg(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "healer",
            "respond",
            "--action",
            "restart_with_env",
            "--set",
            "BATCH_SIZE=4",
            "--set",
            "NUM_WORKERS=2",
            "--diagnosis",
            "OOM at step 8450",
        ],
    )
    assert result.exit_code == 0
    response = tmp_path / ".autosentry" / "recovery_response.md"
    assert response.is_file()
    body = response.read_text()
    assert "ACTION: restart_with_env" in body
    assert "SET: BATCH_SIZE=4" in body
    assert "SET: NUM_WORKERS=2" in body
    assert "OOM at step 8450" in body


def test_healer_respond_rejects_bogus_action(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_cfg(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["healer", "respond", "--action", "bogus"])
    assert result.exit_code == 2


def test_healer_respond_ignores_set_for_non_env_actions(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_cfg(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["healer", "respond", "--action", "restart", "--set", "FOO=bar"],
    )
    assert result.exit_code == 0
    response = (tmp_path / ".autosentry" / "recovery_response.md").read_text()
    assert "ACTION: restart" in response
    assert "SET:" not in response  # --set silently dropped for non-env actions
