"""Skills installer tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from autosentry import skills


def test_targets_for_all_returns_every_tool():
    targets = skills.targets_for(None)
    assert {t.tool for t in targets} == set(skills.ALL_TOOLS)


def test_targets_for_subset_preserves_order_and_dedupes():
    out = skills.targets_for(["claude", "cursor", "claude"])
    assert [t.tool for t in out] == ["claude", "cursor"]


def test_targets_for_unknown_raises():
    with pytest.raises(ValueError, match="unknown tool"):
        skills.targets_for(["bogus"])  # type: ignore[arg-type]


def test_install_writes_expected_files(tmp_path: Path):
    results = skills.install(tmp_path, tools=["claude", "agents"])
    assert {r.status for r in results} == {"created"}
    assert (tmp_path / ".claude" / "commands" / "autosentry.md").is_file()
    assert (tmp_path / "AGENTS.md").is_file()
    # Sanity: the skill body has the install one-liner.
    claude_skill = (tmp_path / ".claude" / "commands" / "autosentry.md").read_text()
    assert "autosentry" in claude_skill
    assert "/autosentry" in claude_skill


def test_install_skips_existing_without_force(tmp_path: Path):
    skills.install(tmp_path, tools=["claude"])
    again = skills.install(tmp_path, tools=["claude"])
    assert [r.status for r in again] == ["skipped_exists"]


def test_install_overwrites_with_force(tmp_path: Path):
    skills.install(tmp_path, tools=["claude"])
    (tmp_path / ".claude" / "commands" / "autosentry.md").write_text("stale")
    out = skills.install(tmp_path, tools=["claude"], force=True)
    assert [r.status for r in out] == ["overwritten"]
    body = (tmp_path / ".claude" / "commands" / "autosentry.md").read_text()
    assert "stale" not in body


def test_install_all_default_drops_every_skill(tmp_path: Path):
    results = skills.install(tmp_path)
    assert {r.target.tool for r in results} == set(skills.ALL_TOOLS)
    for r in results:
        assert r.written.is_file()


def test_install_new_tool_wrappers_land_at_expected_paths(tmp_path: Path):
    skills.install(tmp_path, tools=["aider", "continue", "windsurf", "zed"])
    assert (tmp_path / ".aider.conf.yml").is_file()
    assert (tmp_path / ".continue" / "config.json").is_file()
    assert (tmp_path / ".windsurfrules").is_file()
    assert (tmp_path / ".zed" / "prompts" / "autosentry.md").is_file()


def test_aider_conf_pins_agents_md_as_read_file(tmp_path: Path):
    skills.install(tmp_path, tools=["aider"])
    body = (tmp_path / ".aider.conf.yml").read_text()
    assert "AGENTS.md" in body
    assert "read:" in body


def test_continue_config_has_custom_command(tmp_path: Path):
    import json as _json

    skills.install(tmp_path, tools=["continue"])
    data = _json.loads((tmp_path / ".continue" / "config.json").read_text())
    cmds = data.get("customCommands", [])
    assert any(c.get("name") == "autosentry" for c in cmds)


def test_windsurf_rules_includes_autosentry_routing(tmp_path: Path):
    skills.install(tmp_path, tools=["windsurf"])
    body = (tmp_path / ".windsurfrules").read_text()
    assert "autosentry" in body
    assert "AGENTS.md" in body


def test_zed_prompt_lives_under_dot_zed(tmp_path: Path):
    skills.install(tmp_path, tools=["zed"])
    body = (tmp_path / ".zed" / "prompts" / "autosentry.md").read_text()
    assert "/autosentry" in body


# ----- Phase 5.6 — init skill + scope flag ---------------------------------


def test_install_init_skill_drops_dash_init_files(tmp_path: Path):
    skills.install(tmp_path, tools=["claude", "cursor"], skill_name="init")
    assert (tmp_path / ".claude" / "commands" / "autosentry-init.md").is_file()
    assert (tmp_path / ".cursor" / "commands" / "autosentry-init.md").is_file()
    body = (tmp_path / ".claude" / "commands" / "autosentry-init.md").read_text()
    assert "autosentry-init" in body
    # And the full /autosentry skill is NOT created when --skill init is selected.
    assert not (tmp_path / ".claude" / "commands" / "autosentry.md").exists()


def test_install_skill_all_drops_both(tmp_path: Path):
    skills.install(tmp_path, tools=["claude"], skill_name="all")
    assert (tmp_path / ".claude" / "commands" / "autosentry.md").is_file()
    assert (tmp_path / ".claude" / "commands" / "autosentry-init.md").is_file()


def test_install_global_scope_writes_to_home(tmp_path: Path, monkeypatch):
    """--scope global writes into the (mocked) user home, not the target."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    # Reload the skills module so its _home() captures the patched Path.home().
    import importlib

    import autosentry.skills as _skills_mod

    _skills_mod = importlib.reload(_skills_mod)

    results = _skills_mod.install(
        tmp_path,
        tools=["claude"],
        scope="global",
    )
    expected = fake_home / ".claude" / "commands" / "autosentry.md"
    assert expected.is_file()
    assert all(r.status == "created" for r in results)


def test_install_global_scope_skips_agents(tmp_path: Path, monkeypatch):
    """The `agents` tool has no global slot — AGENTS.md is repo-specific.
    Under --scope global it should produce skipped_no_global, not a write."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    import importlib

    import autosentry.skills as _skills_mod

    _skills_mod = importlib.reload(_skills_mod)

    results = _skills_mod.install(tmp_path, tools=["agents"], scope="global")
    assert [r.status for r in results] == ["skipped_no_global"]
    # And nothing was actually written under the fake home.
    assert not (fake_home / "AGENTS.md").exists()


def test_list_includes_both_skills():
    """`skills_list` (and targets_for(skill_name='all')) covers both skills."""
    targets = skills.targets_for(None, skill_name="all")
    skill_set = {t.skill for t in targets}
    assert skill_set == {"autosentry", "init"}
