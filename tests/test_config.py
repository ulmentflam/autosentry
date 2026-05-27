"""Config loading + validation tests."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from autosentry.config import load_config


def test_load_minimal_config(minimal_config):
    cfg = minimal_config
    assert cfg.process.kind == "local"
    assert cfg.process.command == ["true"]
    assert cfg.process.restart_policy.max_restarts == 2
    assert len(cfg.detectors) == 3
    assert len(cfg.rules) == 1


def test_env_interpolation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTOSENTRY_TEST_VAR", "hello")
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            """\
            process:
              kind: local
              command: ["echo", "$AUTOSENTRY_TEST_VAR"]
              env:
                MY_VAR: "${AUTOSENTRY_TEST_VAR}-world"
            """
        )
    )
    cfg = load_config(cfg_path)
    assert cfg.process.command == ["echo", "hello"]
    assert cfg.process.env["MY_VAR"] == "hello-world"


def test_missing_config_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_invalid_regex_rejected(tmp_path: Path):
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            """\
            process:
              kind: local
              command: ["true"]
            detectors:
              - kind: pattern
                regex: "[unclosed"
            """
        )
    )
    # Pydantic raises ValidationError; we catch the broader hierarchy.
    with pytest.raises((ValueError, Exception)):  # noqa: B017
        load_config(cfg_path)


def test_resolve_relative_paths(minimal_config, tmp_path: Path):
    cfg = minimal_config
    # state_path is relative; resolved against config's directory.
    resolved = cfg.resolve(cfg.state_path)
    assert resolved.is_absolute()
    assert resolved.parent == tmp_path / ".autosentry"
