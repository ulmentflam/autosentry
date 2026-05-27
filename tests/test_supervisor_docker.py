"""Docker supervisor unit tests.

Same shape as the SLURM tests — we don't need a docker daemon running
to validate the command-construction and config-plumbing parts that
tend to drift.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from autosentry.config import load_config
from autosentry.supervisors import supervisor_for
from autosentry.supervisors.docker import DockerSupervisor


def _cfg_with(extra_block: str, tmp_path: Path):
    cfg_path = tmp_path / "autosentry.yaml"
    cfg_path.write_text(
        dedent(
            f"""\
            process:
              kind: docker
              command: ["docker", "run", "-d", "--name", "{{name}}", "img:latest"]
              extra:
                {extra_block}
            """
        )
    )
    return load_config(cfg_path)


def test_docker_supervisor_uses_explicit_container_name(tmp_path: Path):
    cfg = _cfg_with('container_name: "my-job"', tmp_path)
    sup = supervisor_for(cfg, log_dir=tmp_path / "logs")
    assert isinstance(sup, DockerSupervisor)
    assert sup.container_name == "my-job"


def test_docker_supervisor_generates_name_when_missing(tmp_path: Path):
    cfg = _cfg_with("# no container_name", tmp_path)
    sup = supervisor_for(cfg, log_dir=tmp_path / "logs")
    assert isinstance(sup, DockerSupervisor)
    assert sup.container_name.startswith("autosentry-")
    assert len(sup.container_name) > len("autosentry-")  # has random suffix


def test_substitute_name_replaces_token(tmp_path: Path):
    cfg = _cfg_with('container_name: "x1"', tmp_path)
    sup = supervisor_for(cfg, log_dir=tmp_path / "logs")
    assert isinstance(sup, DockerSupervisor)
    out = sup._substitute_name(["docker", "logs", "-f", "{name}"])
    assert out == ["docker", "logs", "-f", "x1"]


def test_default_log_stream_command_used_when_not_overridden(tmp_path: Path):
    cfg = _cfg_with('container_name: "x"', tmp_path)
    sup = supervisor_for(cfg, log_dir=tmp_path / "logs")
    assert isinstance(sup, DockerSupervisor)
    # Confirm the extra dict didn't acquire the default by mistake — the
    # default lives in the supervisor, not the config.
    assert "log_stream_command" not in sup.extra


def test_status_reports_not_running_when_container_absent(tmp_path: Path):
    cfg = _cfg_with('container_name: "definitely-not-a-real-container-name-xyz"', tmp_path)
    sup = supervisor_for(cfg, log_dir=tmp_path / "logs")
    assert isinstance(sup, DockerSupervisor)
    # We didn't start anything, so this should be not-running. Either
    # docker is missing on the test host (FileNotFoundError → False) or
    # docker says no such container (False).
    assert sup.status().running is False
