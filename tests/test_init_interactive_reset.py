"""Interactive init: prompt to reset / upgrade / cancel when config exists.

The non-interactive paths are exercised in test_cli.py. These tests
cover the new interactive-only branch added so operators inside Claude
Code (or any TTY) get a choice instead of a hard `exit 1` when running
`autosentry init` over an existing config.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from autosentry.cli import app
from autosentry.cli.commands import init as init_mod

runner = CliRunner()


@pytest.fixture
def existing_repo(tmp_path: Path):
    """A repo that already has .autosentry/autosentry.yaml — the scenario
    a user runs `init` again over."""
    autosentry_dir = tmp_path / ".autosentry"
    autosentry_dir.mkdir()
    yaml_dst = autosentry_dir / "autosentry.yaml"
    yaml_dst.write_text("# user-customized config — must not be silently wiped\n")
    return tmp_path, yaml_dst


def test_interactive_reset_choice_overwrites_existing_yaml(existing_repo, monkeypatch):
    target, yaml_dst = existing_repo
    # Pretend the session is interactive AND the user picked 'r' (reset).
    monkeypatch.setattr(init_mod, "is_interactive", lambda: True)
    monkeypatch.setattr(init_mod, "ask", lambda *a, **k: "r")
    # Skip the deep interactive walkthrough (stack detection + skills install).
    monkeypatch.setattr(init_mod, "_interactive_fill", lambda *a, **k: None)
    monkeypatch.setattr(init_mod, "_resolve_install_skills", lambda *a, **k: "none")

    result = runner.invoke(app, ["init", str(target)])

    assert result.exit_code == 0, result.stdout
    # The user's stale content is gone; replaced by the fresh template.
    body = yaml_dst.read_text()
    assert "user-customized" not in body
    assert "process:" in body  # the template's hallmark


def test_interactive_upgrade_choice_preserves_existing_yaml(existing_repo, monkeypatch):
    target, yaml_dst = existing_repo
    # Existing file isn't a real config — make it look like one so the
    # upgrade flow's YAML parse doesn't return None.
    yaml_dst.write_text(
        "process:\n  command: ['python', 'app.py']\n  restart_policy:\n    max_restarts: 5\n"
    )
    monkeypatch.setattr(init_mod, "is_interactive", lambda: True)
    monkeypatch.setattr(init_mod, "ask", lambda *a, **k: "u")

    result = runner.invoke(app, ["init", str(target)])

    assert result.exit_code == 0, result.stdout
    # The user's command line survives — upgrade only touches scalar
    # tuning defaults, never user-personal data like process.command.
    body = yaml_dst.read_text()
    assert "'python'" in body or "python" in body
    assert "app.py" in body


def test_interactive_cancel_choice_leaves_yaml_untouched(existing_repo, monkeypatch):
    target, yaml_dst = existing_repo
    monkeypatch.setattr(init_mod, "is_interactive", lambda: True)
    monkeypatch.setattr(init_mod, "ask", lambda *a, **k: "c")

    result = runner.invoke(app, ["init", str(target)])

    assert result.exit_code != 0  # explicit abort
    assert yaml_dst.read_text().startswith("# user-customized")


def test_non_interactive_still_exits_with_helpful_message(existing_repo):
    target, _ = existing_repo
    # No TTY → no prompt → falls through to the legacy exit-1 path.
    result = runner.invoke(app, ["init", str(target), "--non-interactive"])
    assert result.exit_code != 0
    assert "--force" in result.stdout or "--upgrade" in result.stdout
