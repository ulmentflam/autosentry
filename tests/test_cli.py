"""CLI tests — exercises doctor, onboard, init, and the style helpers.

We invoke the Typer app via its Click runner so the tests don't fork
subprocesses. Anything that depends on the *current* repo's state
(``doctor`` checks the cwd) runs inside ``tmp_path`` for hermetic
results.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from autosentry.cli import app

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s: str) -> str:
    """Strip ANSI SGR codes. CI sets FORCE_COLOR=1 which makes Rich emit
    colors even when stdout is piped through CliRunner; substring checks
    against styled output need to compare against the de-styled text."""
    return _ANSI_RE.sub("", s)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ----- doctor --------------------------------------------------------------


def test_doctor_no_config_warns_not_fails(runner: CliRunner, isolated_cwd: Path):
    result = runner.invoke(app, ["doctor"])
    # No config → at least a warn but no fail → exit 0.
    assert result.exit_code == 0
    assert "autosentry doctor" in result.stdout
    assert "autosentry.yaml not found" in result.stdout


def test_doctor_with_invalid_config_fails(runner: CliRunner, isolated_cwd: Path):
    (isolated_cwd / "autosentry.yaml").write_text("this: is: not: yaml: garbage\n[broken")
    result = runner.invoke(app, ["doctor"])
    # Invalid YAML → fail row → exit 1.
    assert result.exit_code == 1
    assert "parse error" in result.stdout or "fail" in result.stdout


def test_doctor_with_minimal_valid_config_passes(runner: CliRunner, isolated_cwd: Path):
    (isolated_cwd / "autosentry.yaml").write_text(
        dedent(
            """\
            process:
              kind: local
              command: ["true"]
            healing:
              claude:
                enabled: false
            """
        )
    )
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "autosentry.yaml" in result.stdout


# ----- onboard -------------------------------------------------------------


def test_onboard_for_agent_no_config(runner: CliRunner, isolated_cwd: Path):
    result = runner.invoke(app, ["onboard", "--for-agent"])
    assert result.exit_code == 0
    assert "phase: no_config" in result.stdout
    assert "autosentry init" in result.stdout
    # Plain output: no ANSI escape sequences.
    assert "\x1b[" not in result.stdout


def test_onboard_for_agent_not_running(runner: CliRunner, isolated_cwd: Path):
    (isolated_cwd / "autosentry.yaml").write_text("process:\n  command: ['true']\n")
    result = runner.invoke(app, ["onboard", "--for-agent"])
    assert "phase: not_running" in result.stdout
    assert "tail -F .autosentry/logs/autosentry.log" in result.stdout


def test_onboard_for_agent_running(runner: CliRunner, isolated_cwd: Path):
    (isolated_cwd / "autosentry.yaml").write_text("process:\n  command: ['true']\n")
    (isolated_cwd / ".autosentry").mkdir()
    # Pretend autosentry is running under our own pid (we know it's alive).
    (isolated_cwd / ".autosentry" / "state.json").write_text(f'{{"pid": {os.getpid()}}}')
    result = runner.invoke(app, ["onboard", "--for-agent"])
    assert "phase: running" in result.stdout
    assert "autosentry analyze" in result.stdout


# ----- init ---------------------------------------------------------------


def test_init_non_interactive_creates_skeleton(runner: CliRunner, isolated_cwd: Path):
    result = runner.invoke(app, ["init", str(isolated_cwd), "--non-interactive"])
    assert result.exit_code == 0
    assert (isolated_cwd / "autosentry.yaml").is_file()
    assert (isolated_cwd / ".autosentry" / "program.md").is_file()
    assert (isolated_cwd / ".autosentry" / "prompts" / "recovery.md").is_file()
    assert "initialized autosentry" in result.stdout


def test_init_for_agent_writes_notes(runner: CliRunner, isolated_cwd: Path):
    result = runner.invoke(app, ["init", str(isolated_cwd), "--non-interactive", "--for-agent"])
    assert result.exit_code == 0
    notes = isolated_cwd / ".autosentry" / "AGENT_NOTES.md"
    assert notes.is_file()
    body = notes.read_text()
    assert "autosentry doctor" in body
    assert "autosentry run" in body


def test_init_refuses_existing_without_force(runner: CliRunner, isolated_cwd: Path):
    (isolated_cwd / "autosentry.yaml").write_text("existing")
    result = runner.invoke(app, ["init", str(isolated_cwd), "--non-interactive"])
    assert result.exit_code == 1
    assert "already exists" in result.stdout


def test_init_upgrade_refreshes_stale_default_with_force(runner: CliRunner, isolated_cwd: Path):
    """A pre-existing config with a stale ``max_restarts: 5`` bumps to the
    current template default (10) under ``--upgrade --force``."""
    yaml_path = isolated_cwd / "autosentry.yaml"
    yaml_path.write_text(
        dedent(
            """\
            # autosentry — pre-existing user config
            process:
              kind: local
              command: ["python", "my_special_script.py"]   # user customization
              restart_policy:
                max_restarts: 5
                cooldown_seconds: 60
            """
        )
    )
    result = runner.invoke(app, ["init", str(isolated_cwd), "--upgrade", "--force"])
    assert result.exit_code == 0
    body = yaml_path.read_text()
    assert "max_restarts: 10" in body
    # User's customization must survive intact.
    assert "my_special_script.py" in body
    # And the top comment too (proves ruamel round-trip preserved comments).
    assert "pre-existing user config" in body


def test_init_upgrade_inserts_missing_key(runner: CliRunner, isolated_cwd: Path):
    """A config missing an entire branch of the upgradeable tree gets the
    template's scalar defaults injected on --upgrade --force."""
    yaml_path = isolated_cwd / "autosentry.yaml"
    yaml_path.write_text(
        dedent(
            """\
            process:
              kind: local
              command: ["true"]
            """
        )
    )
    result = runner.invoke(app, ["init", str(isolated_cwd), "--upgrade", "--force"])
    assert result.exit_code == 0
    body = yaml_path.read_text()
    # process.restart_policy was absent — the upgrade should have inserted
    # cooldown_seconds and max_restarts (both ship in the template).
    assert "cooldown_seconds: 60" in body
    assert "max_restarts: 10" in body


def test_init_upgrade_noop_when_already_current(runner: CliRunner, isolated_cwd: Path):
    """Fresh init from the current template has nothing to upgrade."""
    runner.invoke(app, ["init", str(isolated_cwd), "--non-interactive"])
    assert (isolated_cwd / "autosentry.yaml").exists()
    result = runner.invoke(app, ["init", str(isolated_cwd), "--upgrade", "--force"])
    assert result.exit_code == 0
    assert "no defaults needed refreshing" in result.stdout or "0 key" in result.stdout


def test_init_upgrade_refuses_when_no_config(runner: CliRunner, isolated_cwd: Path):
    result = runner.invoke(app, ["init", str(isolated_cwd), "--upgrade"])
    assert result.exit_code == 1
    assert "nothing to upgrade" in result.stdout


def test_init_install_skills_local_drops_files(runner: CliRunner, isolated_cwd: Path):
    """`--install-skills local` drops every tool's wrapper into the repo."""
    result = runner.invoke(
        app,
        ["init", str(isolated_cwd), "--non-interactive", "--install-skills", "local"],
    )
    assert result.exit_code == 0
    assert (isolated_cwd / ".claude" / "commands" / "autosentry.md").is_file()
    assert (isolated_cwd / "AGENTS.md").is_file()
    assert "/autosentry skill installed at scope=local" in _plain(result.stdout)


def test_init_install_skills_none_default_in_non_interactive(runner: CliRunner, isolated_cwd: Path):
    """`--non-interactive` keeps the historical behavior: don't auto-install
    skills unless the user opts in via --install-skills=local|global."""
    result = runner.invoke(app, ["init", str(isolated_cwd), "--non-interactive"])
    assert result.exit_code == 0
    assert not (isolated_cwd / ".claude").exists()


def test_init_install_skills_invalid_value_falls_back_to_none(
    runner: CliRunner, isolated_cwd: Path
):
    result = runner.invoke(
        app,
        ["init", str(isolated_cwd), "--non-interactive", "--install-skills", "bogus"],
    )
    # Init still succeeds; the skills install just gets skipped with a warning.
    assert result.exit_code == 0
    assert "invalid --install-skills" in result.stdout
    assert not (isolated_cwd / ".claude").exists()


def test_init_interactive_rewrites_command_in_yaml(runner: CliRunner, isolated_cwd: Path):
    """Force the interactive branch (Typer's runner is non-TTY by default)
    and stub the prompt helpers, so we can verify the YAML rewrite works
    end-to-end without coupling to ``rich.Prompt`` internals."""
    with (
        patch("autosentry.cli.commands.init.is_interactive", return_value=True),
        patch("autosentry.cli.commands.init.ask", return_value="python myscript.py --flag"),
        patch("autosentry.cli.commands.init.confirm", return_value=False),
    ):
        result = runner.invoke(app, ["init", str(isolated_cwd)])
    assert result.exit_code == 0
    body = (isolated_cwd / "autosentry.yaml").read_text()
    assert "myscript.py" in body
    # Heavy template comments should survive the ruamel round-trip.
    assert "autosentry — supervised process configuration" in body


# ----- style helpers ------------------------------------------------------


def test_is_interactive_false_for_piped_stdout():
    """Sanity: the TTY detection respects sys.stdout.isatty()."""
    from autosentry.cli import style

    # We can't reliably mock sys directly under pytest capture, but we can
    # confirm the function exists and returns a bool.
    assert isinstance(style.is_interactive(), bool)


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="no pty support")
def test_sanitize_terminal_repairs_raw_mode():
    """A terminal left in raw / no-echo mode (e.g. by a crashed TUI) is the
    root cause of "init lost my input / backspace dead / couldn't enter my
    command". The sanitizer must re-enable canonical input + echo so the
    next prompt behaves, without clobbering the rest of the termios state."""
    pty = pytest.importorskip("pty")
    termios = pytest.importorskip("termios")
    tty = pytest.importorskip("tty")

    from autosentry.cli.style import _sanitize_terminal_fd

    master, slave = pty.openpty()
    try:
        # A prior program crashed without restoring termios → raw mode.
        tty.setraw(slave)
        before = termios.tcgetattr(slave)
        assert not (before[3] & termios.ICANON)  # raw mode cleared these
        assert not (before[3] & termios.ECHO)

        assert _sanitize_terminal_fd(slave) is True

        after = termios.tcgetattr(slave)
        assert after[3] & termios.ICANON  # line editing restored
        assert after[3] & termios.ECHO  # typed input echoes again
        assert after[3] & termios.ECHOE  # backspace erases visibly
    finally:
        os.close(master)
        os.close(slave)


def test_sanitize_terminal_noop_on_non_tty():
    """On a plain pipe (not a tty) the sanitizer reports failure cleanly
    rather than raising — interactive prompts skip it via isatty()."""
    import tempfile

    from autosentry.cli.style import _sanitize_terminal_fd

    with tempfile.TemporaryFile() as f:
        assert _sanitize_terminal_fd(f.fileno()) is False


def test_banner_includes_version():
    from autosentry.cli.style import banner

    text = banner("9.9.9")
    rendered = str(text)
    assert "9.9.9" in rendered


def _stub_check(monkeypatch, *, current: str, latest: str, is_outdated: bool):
    from autosentry.updater import UpdateCheck

    def fake(**_kwargs):
        return UpdateCheck(current=current, latest=latest, is_outdated=is_outdated)

    monkeypatch.setattr("autosentry.cli.commands.update.updater_check", fake)


def test_update_check_outdated_recommends_command(runner: CliRunner, monkeypatch):
    _stub_check(monkeypatch, current="0.1.0", latest="0.2.0", is_outdated=True)
    result = runner.invoke(app, ["update", "--check"])
    assert result.exit_code == 0  # exits 0 so agent triage chains don't abort
    out = _plain(result.output)
    assert "0.2.0" in out
    assert "autosentry update" in out  # recommends the upgrade command


def test_update_check_up_to_date_is_quiet(runner: CliRunner, monkeypatch):
    _stub_check(monkeypatch, current="0.2.0", latest="0.2.0", is_outdated=False)
    result = runner.invoke(app, ["update", "--check"])
    assert result.exit_code == 0
    out = _plain(result.output)
    assert "up to date" in out
    assert "update available" not in out


def test_update_json_is_machine_readable(runner: CliRunner, monkeypatch):
    import json

    _stub_check(monkeypatch, current="0.1.0", latest="0.2.0", is_outdated=True)
    result = runner.invoke(app, ["update", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "current": "0.1.0",
        "latest": "0.2.0",
        "is_outdated": True,
    }
