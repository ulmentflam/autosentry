"""SLURM supervisor unit tests.

We don't shell out to a real SLURM here — these tests pin the parser,
the state mapping, and the template-substitution helpers, which are the
parts most likely to drift.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from autosentry.config import load_config
from autosentry.supervisors.slurm import (
    _COMPLETED_STATES,
    _FAILED_STATES,
    _JOB_ID_RE,
    _RUNNING_STATES,
    _exit_code_for_failed,
    _fmt,
    _pid_from_job,
)


def test_job_id_parsed_from_sbatch_output():
    text = "Submitted batch job 12345\n"
    m = _JOB_ID_RE.search(text)
    assert m is not None
    assert m.group(1) == "12345"


def test_job_id_parsed_from_multiline_with_warnings():
    text = "sbatch: warning: --extra-flag deprecated\nSubmitted batch job 9001\n"
    m = _JOB_ID_RE.search(text)
    assert m is not None
    assert m.group(1) == "9001"


def test_fmt_substitutes_job_id():
    assert _fmt("logs/job_{job_id}.log", "42") == "logs/job_42.log"
    assert _fmt("static", "42") == "static"
    assert _fmt("logs/job_{job_id}.log", None) == "logs/job_{job_id}.log"


def test_running_and_terminal_state_sets_are_disjoint():
    assert _RUNNING_STATES.isdisjoint(_FAILED_STATES)
    assert _RUNNING_STATES.isdisjoint(_COMPLETED_STATES)
    assert _COMPLETED_STATES.isdisjoint(_FAILED_STATES)


def test_pid_from_job_returns_int_or_none():
    assert _pid_from_job("123") == 123
    assert _pid_from_job(None) is None
    assert _pid_from_job("not-a-number") is None


def test_failed_state_maps_to_nonzero_exit():
    assert _exit_code_for_failed("TIMEOUT") == 124
    assert _exit_code_for_failed("OUT_OF_MEMORY") == 137
    assert _exit_code_for_failed("FAILED") == 1
    assert _exit_code_for_failed("UNKNOWN_STATE") == 1


def test_supervisor_factory_resolves_slurm_kind(tmp_path: Path):
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            """\
            process:
              kind: slurm
              command: ["sbatch", "submit.sh"]
              extra:
                log_pattern: "out_{job_id}.log"
            """
        )
    )
    cfg = load_config(cfg_path)
    # Import here so test stays isolated even if slurm subprocess deps blow up.
    from autosentry.supervisors import supervisor_for
    from autosentry.supervisors.slurm import SlurmSupervisor

    sup = supervisor_for(cfg, log_dir=tmp_path / "logs")
    assert isinstance(sup, SlurmSupervisor)
    # extra is plumbed through from yaml.
    assert sup.extra["log_pattern"] == "out_{job_id}.log"
