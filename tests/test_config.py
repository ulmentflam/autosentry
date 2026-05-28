"""Config loading + validation tests."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from autosentry.config import (
    DEFAULT_CONFIG_PATH,
    load_config,
    resolve_existing_config,
)

_MINIMAL = 'process:\n  kind: local\n  command: ["true"]\n  cwd: "."\n'


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
    # state_path is relative; resolved against the project root.
    resolved = cfg.resolve(cfg.state_path)
    assert resolved.is_absolute()
    assert resolved.parent == tmp_path / ".autosentry"


def test_resolve_anchors_on_project_root_for_autosentry_config(tmp_path: Path):
    """When the config lives at ``<root>/.autosentry/autosentry.yaml``, every
    relative path still resolves against ``<root>`` — not ``.autosentry/`` —
    so nothing double-nests and snapshots/repo-root stay correct."""
    (tmp_path / ".autosentry").mkdir()
    (tmp_path / ".autosentry" / "autosentry.yaml").write_text(_MINIMAL)
    cfg = load_config(tmp_path / ".autosentry" / "autosentry.yaml")

    assert cfg._project_root() == tmp_path.resolve()
    # Runtime dirs nest once under .autosentry/, not twice.
    assert cfg.resolve(cfg.state_path) == (tmp_path / ".autosentry" / "state.json").resolve()
    assert cfg.resolve(cfg.incidents_dir) == (tmp_path / ".autosentry" / "incidents").resolve()
    # Config snapshots + the healer's repo root anchor on the repo, not .autosentry/.
    assert cfg.resolve("pyproject.toml") == (tmp_path / "pyproject.toml").resolve()
    assert cfg.resolve(".") == tmp_path.resolve()


def test_legacy_root_config_is_loaded_as_fallback(tmp_path: Path, monkeypatch):
    """Asking for the new default path falls back to a pre-0.8 root-level
    ``autosentry.yaml`` so existing repos keep working without migration."""
    (tmp_path / "autosentry.yaml").write_text(_MINIMAL)
    monkeypatch.chdir(tmp_path)

    assert resolve_existing_config(DEFAULT_CONFIG_PATH) == Path("autosentry.yaml")
    cfg = load_config(DEFAULT_CONFIG_PATH)
    # The legacy config sits at the repo root → project root is the repo.
    assert cfg._project_root() == tmp_path.resolve()
    assert cfg.resolve(cfg.state_path) == (tmp_path / ".autosentry" / "state.json").resolve()


def test_resolve_existing_config_returns_none_when_absent(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_existing_config(DEFAULT_CONFIG_PATH) is None
