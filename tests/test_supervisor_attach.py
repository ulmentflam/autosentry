"""Attach supervisor unit tests."""

from __future__ import annotations

import os
import time
from pathlib import Path
from textwrap import dedent

import pytest

from autosentry.config import RuleAction, load_config
from autosentry.supervisors import supervisor_for
from autosentry.supervisors.attach import AttachSupervisor, _pid_alive, _resolve_pid
from autosentry.supervisors.base import SupervisorError


def _cfg(tmp_path: Path, extra_block: str) -> Path:
    p = tmp_path / "autosentry.yaml"
    p.write_text(
        dedent(
            f"""\
            process:
              kind: attach
              command: []
              extra:
                {extra_block}
            """
        )
    )
    return p


def test_resolve_pid_from_int():
    assert _resolve_pid({"pid": 42}) == 42
    assert _resolve_pid({"pid": "42"}) == 42
    assert _resolve_pid({}) is None


def test_resolve_pid_from_pid_file(tmp_path: Path):
    pf = tmp_path / "x.pid"
    pf.write_text("12345\n")
    assert _resolve_pid({"pid_file": str(pf)}) == 12345


def test_resolve_pid_invalid_raises():
    with pytest.raises(SupervisorError):
        _resolve_pid({"pid": "definitely-not-a-number"})


def test_pid_alive_for_self():
    assert _pid_alive(os.getpid())


def test_pid_alive_for_unlikely_pid():
    # PIDs above the typical pid_max on macOS/Linux that won't exist.
    assert not _pid_alive(99_999_999)


def test_start_without_pid_raises(tmp_path: Path):
    cfg_path = _cfg(tmp_path, "log_path: /tmp/x.log")
    cfg = load_config(cfg_path)
    sup = supervisor_for(cfg, log_dir=tmp_path / "logs")
    assert isinstance(sup, AttachSupervisor)
    with pytest.raises(SupervisorError):
        sup.start()


def test_start_with_dead_pid_returns_not_running(tmp_path: Path):
    cfg_path = _cfg(tmp_path, "pid: 99999999")
    cfg = load_config(cfg_path)
    sup = supervisor_for(cfg, log_dir=tmp_path / "logs")
    assert isinstance(sup, AttachSupervisor)
    status = sup.start()
    assert status.running is False


def test_restart_action_rejected(tmp_path: Path):
    cfg_path = _cfg(tmp_path, f"pid: {os.getpid()}")
    cfg = load_config(cfg_path)
    sup = supervisor_for(cfg, log_dir=tmp_path / "logs")
    assert isinstance(sup, AttachSupervisor)
    with pytest.raises(SupervisorError):
        sup.apply_action(RuleAction(kind="restart"))


def test_log_tail_picks_up_new_lines(tmp_path: Path):
    log_file = tmp_path / "watched.log"
    log_file.write_text("preexisting line — should NOT be replayed\n")

    # Write the YAML ourselves so the multi-key extras block has clean
    # indentation — the _cfg helper's f-string trick mangles multi-line
    # block content.
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            f"""\
            process:
              kind: attach
              command: []
              extra:
                pid: {os.getpid()}
                log_path: {log_file}
            """
        )
    )
    cfg = load_config(cfg_path)
    sup = supervisor_for(cfg, log_dir=tmp_path / "logs")
    assert isinstance(sup, AttachSupervisor)
    sup.start()

    try:
        # Append a fresh line and wait for the tail thread to catch it.
        with log_file.open("a", encoding="utf-8") as f:
            f.write("freshly written\n")

        iterator = sup.iter_log_lines()
        deadline = time.time() + 5
        seen: list[str] = []
        while time.time() < deadline and not seen:
            item = next(iterator)
            if item is not None:
                seen.append(item.text)
        assert seen, "expected a log line to be tailed"
        assert any("freshly written" in s for s in seen)
        # Pre-existing line should NOT have been replayed.
        assert not any("preexisting" in s for s in seen)
    finally:
        sup.stop()
