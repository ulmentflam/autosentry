"""Tests for the expanded `autosentry doctor` (0.12.0).

Covers the four new check categories — layout & migration, corrupt
state files, integration health, steering — plus the new ``--fix``
auto-repair flag.

Pattern follows ``tests/test_cli.py``: CliRunner unit-style + tmp_path
fixtures. Tests don't try to assert exact rendered text; they look for
status keywords and verify file system effects of repairs.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest
from typer.testing import CliRunner

from autosentry.cli import app

runner = CliRunner()


def _write_config(
    tmp_path: Path,
    *,
    vault: bool = True,
    dispatch_mode: str = "builtin",
    langgraph: bool = False,
    legacy_mode: str | None = None,
) -> Path:
    """Write a minimal `.autosentry/autosentry.yaml`. Returns its absolute path."""
    autosentry_dir = tmp_path / ".autosentry"
    autosentry_dir.mkdir(parents=True, exist_ok=True)
    (autosentry_dir / "incidents").mkdir(exist_ok=True)
    (autosentry_dir / "logs").mkdir(exist_ok=True)
    (autosentry_dir / "prompts").mkdir(exist_ok=True)
    cfg_path = autosentry_dir / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            f"""\
            process:
              kind: local
              command: ["true"]
            healing:
              claude:
                enabled: false
              langgraph:
                enabled: {str(langgraph).lower()}
                provider: anthropic
                model: claude-haiku-4-5
            vault:
              enabled: {str(vault).lower()}
            dispatch:
              mode: {dispatch_mode}
            state_path: ".autosentry/state.json"
            incidents_dir: ".autosentry/incidents"
            """
        )
    )
    # Inject ``healing.claude.mode`` after the file is written so the
    # indentation lines up with the rest of the dedented block.
    if legacy_mode:
        text = cfg_path.read_text()
        cfg_path.write_text(
            text.replace("    enabled: false\n", f"    enabled: false\n    mode: {legacy_mode}\n")
        )
    return cfg_path


def _invoke_doctor(tmp_path: Path, *args: str):
    """Run the doctor command with cwd set to ``tmp_path``."""
    import os

    prev_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        return runner.invoke(app, ["doctor", *args])
    finally:
        os.chdir(prev_cwd)


# ---------------------------------------------------------------------------
# Layout & migration
# ---------------------------------------------------------------------------


def test_doctor_flags_legacy_root_config(tmp_path: Path) -> None:
    """A pre-0.8 root-level autosentry.yaml surfaces as a 'legacy
    config' check, even when no .autosentry/ exists yet."""
    (tmp_path / "autosentry.yaml").write_text(
        dedent(
            """\
            process: {kind: local, command: ["true"]}
            healing: {claude: {enabled: false}}
            """
        )
    )
    result = _invoke_doctor(tmp_path)
    # Doctor finds + loads the legacy config, then surfaces the migration nudge.
    assert "legacy config" in result.stdout


def test_doctor_fix_migrates_legacy_root_config(tmp_path: Path) -> None:
    """`--fix` should move the legacy file into `.autosentry/`."""
    legacy = tmp_path / "autosentry.yaml"
    legacy.write_text(
        dedent(
            """\
            process: {kind: local, command: ["true"]}
            healing: {claude: {enabled: false}}
            """
        )
    )
    _invoke_doctor(tmp_path, "--fix")
    assert not legacy.exists(), "legacy config should have been moved"
    assert (tmp_path / ".autosentry" / "autosentry.yaml").exists()


def test_doctor_recreates_missing_subdirs(tmp_path: Path) -> None:
    """If a user `rm -rf .autosentry/incidents`, doctor surfaces the
    warning and `--fix` recreates the dirs."""
    _write_config(tmp_path)
    (tmp_path / ".autosentry" / "incidents").rmdir()
    result = _invoke_doctor(tmp_path)
    assert ".autosentry tree" in result.stdout
    _invoke_doctor(tmp_path, "--fix")
    assert (tmp_path / ".autosentry" / "incidents").is_dir()


def test_doctor_creates_missing_vault_dir(tmp_path: Path) -> None:
    """Vault enabled, but the directory's missing → warn + repair."""
    _write_config(tmp_path, vault=True)
    vault_root = tmp_path / ".autosentry" / "vault"
    assert not vault_root.exists()
    _invoke_doctor(tmp_path, "--fix")
    assert (vault_root / "index.md").exists(), "vault scaffold should have been created"


def test_doctor_rebuilds_vault_from_incidents(tmp_path: Path) -> None:
    """When the vault is missing but `incidents/index.jsonl` has
    entries, --fix replays them into a fresh vault."""
    _write_config(tmp_path, vault=True)
    index = tmp_path / ".autosentry" / "incidents" / "index.jsonl"
    index.write_text(
        "\n".join(
            json.dumps(
                {
                    "id": f"2026-05-31T00-0{i}-00Z-error-oom",
                    "kind": "error",
                    "detector": "oom",
                    "message": "OutOfMemoryError: CUDA",
                }
            )
            for i in range(4)
        )
        + "\n"
    )
    _invoke_doctor(tmp_path, "--fix")
    vault_root = tmp_path / ".autosentry" / "vault"
    assert (vault_root / "index.md").exists()
    incidents = list((vault_root / "incidents").glob("*.md"))
    assert len(incidents) == 4, f"expected 4 rebuilt incidents, got {len(incidents)}"


# ---------------------------------------------------------------------------
# Corrupt state files
# ---------------------------------------------------------------------------


def test_doctor_rotates_corrupt_state_json(tmp_path: Path) -> None:
    _write_config(tmp_path)
    state = tmp_path / ".autosentry" / "state.json"
    state.write_text("{not-json")
    result = _invoke_doctor(tmp_path)
    assert "state.json" in result.stdout and ("fail" in result.stdout or "✗" in result.stdout)
    _invoke_doctor(tmp_path, "--fix")
    assert not state.exists(), "corrupt state.json should have been rotated aside"
    siblings = list((tmp_path / ".autosentry").glob("state.json.broken-*"))
    assert siblings, "expected the rotated-aside file"


def test_doctor_rotates_corrupt_attempts_tsv(tmp_path: Path) -> None:
    _write_config(tmp_path)
    attempts = tmp_path / ".autosentry" / "attempts.tsv"
    attempts.write_text("bad\tdata\n")  # only 2 columns
    _invoke_doctor(tmp_path, "--fix")
    siblings = list((tmp_path / ".autosentry").glob("attempts.tsv.broken-*"))
    assert siblings


def test_doctor_rotates_corrupt_index_jsonl(tmp_path: Path) -> None:
    _write_config(tmp_path)
    index = tmp_path / ".autosentry" / "incidents" / "index.jsonl"
    index.write_text('{"id": "ok"}\n{not json\n')
    _invoke_doctor(tmp_path, "--fix")
    siblings = list((tmp_path / ".autosentry" / "incidents").glob("index.jsonl.broken-*"))
    assert siblings


# ---------------------------------------------------------------------------
# Integration health
# ---------------------------------------------------------------------------


def test_doctor_flags_missing_stop_hook(tmp_path: Path) -> None:
    """When `dispatch.mode: session` but `.claude/settings.local.json`
    is missing, doctor should warn — and `--fix` should install it."""
    _write_config(tmp_path, dispatch_mode="session")
    settings = tmp_path / ".claude" / "settings.local.json"
    assert not settings.exists()
    _invoke_doctor(tmp_path, "--fix")
    assert settings.exists(), "Stop hook settings file should have been created"
    data = json.loads(settings.read_text())
    commands = [h["command"] for group in data["hooks"]["Stop"] for h in group["hooks"]]
    assert any("autosentry probe" in c for c in commands)


def test_doctor_flags_missing_provider_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When langgraph is enabled but no ANTHROPIC_API_KEY is set, doctor
    fails the check. No auto-repair — keys are sensitive."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _write_config(tmp_path, langgraph=True)
    result = _invoke_doctor(tmp_path)
    assert "langgraph api keys" in result.stdout
    assert "ANTHROPIC_API_KEY" in result.stdout


def test_doctor_provider_keys_ok_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    _write_config(tmp_path, langgraph=True)
    result = _invoke_doctor(tmp_path)
    # The line should mention the key is set; the check itself is ok.
    assert "ANTHROPIC_API_KEY" in result.stdout
    assert "langgraph api keys" in result.stdout


def test_doctor_clears_orphan_recovery_request(tmp_path: Path) -> None:
    """A stale recovery_request.md with no response and no live monitor
    should rotate aside."""
    _write_config(tmp_path)
    req = tmp_path / ".autosentry" / "recovery_request.md"
    req.write_text("orphan")
    # Old state.json so doctor judges the monitor as likely-dead.
    state = tmp_path / ".autosentry" / "state.json"
    state.write_text('{"pid": 1, "last_heartbeat": "2020-01-01T00:00:00+00:00"}')
    import os

    old = state.stat().st_mtime - 10_000
    os.utime(state, (old, old))
    _invoke_doctor(tmp_path, "--fix")
    assert not req.exists()
    rotated = list((tmp_path / ".autosentry").glob("recovery_request.md.stale-*"))
    assert rotated


# ---------------------------------------------------------------------------
# Steering nudges
# ---------------------------------------------------------------------------


def test_doctor_steers_away_from_subprocess_mode(tmp_path: Path) -> None:
    _write_config(tmp_path, legacy_mode="subprocess")
    result = _invoke_doctor(tmp_path)
    assert "claude.mode steering" in result.stdout
    # The detail mentions both alternatives.
    assert "dispatch.mode" in result.stdout
    assert "langgraph" in result.stdout


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_doctor_fix_is_idempotent(tmp_path: Path) -> None:
    """Running `--fix` twice should produce no additional changes the
    second time."""
    _write_config(tmp_path)
    # Provoke a couple of repairs.
    (tmp_path / ".autosentry" / "incidents").rmdir()
    (tmp_path / ".autosentry" / "state.json").write_text("{broken")

    _invoke_doctor(tmp_path, "--fix")
    before = sorted(p.name for p in (tmp_path / ".autosentry").iterdir())
    _invoke_doctor(tmp_path, "--fix")
    after = sorted(p.name for p in (tmp_path / ".autosentry").iterdir())
    assert before == after, "second --fix should not change the filesystem"
