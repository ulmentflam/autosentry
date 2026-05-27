"""git_ops tests.

These do create real, throwaway git repos under tmp_path so we exercise
the actual subprocess plumbing. Skipped if git isn't on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from autosentry import git_ops
from autosentry.config import GitConfig

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    # The host's global config may turn on SSH/GPG signing (e.g. via
    # 1Password). The signer won't be reachable in tests, so disable.
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _git(tmp_path, "config", "tag.gpgsign", "false")
    _git(tmp_path, "config", "gpg.format", "openpgp")
    (tmp_path / "file.txt").write_text("hello\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial")
    return tmp_path


def test_is_git_repo(tmp_path: Path, repo: Path):
    assert git_ops.is_git_repo(repo)
    assert not git_ops.is_git_repo(tmp_path.parent / "not_a_repo_xyz")


def test_context_captures_branch_and_commit(repo: Path):
    ctx = git_ops.context(repo)
    assert ctx is not None
    assert ctx.base_branch == "main"
    assert len(ctx.base_commit) == 40  # full sha


def test_create_fix_branch_switches(repo: Path):
    cfg = GitConfig(enabled=True, branch_prefix="autosentry/fix-", auto_merge=False)
    fb = git_ops.create_fix_branch(repo, cfg=cfg, incident_id="inc-1")
    assert fb is not None
    assert fb.name == "autosentry/fix-inc-1"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "autosentry/fix-inc-1"


def test_create_fix_branch_dedup_when_name_taken(repo: Path):
    cfg = GitConfig(enabled=True)
    fb1 = git_ops.create_fix_branch(repo, cfg=cfg, incident_id="inc-1")
    assert fb1 is not None
    # Switch back so the second create starts from main.
    _git(repo, "switch", "main")
    fb2 = git_ops.create_fix_branch(repo, cfg=cfg, incident_id="inc-1")
    assert fb2 is not None
    assert fb2.name == "autosentry/fix-inc-1-1"


def test_create_fix_branch_returns_none_when_not_a_repo(tmp_path: Path):
    cfg = GitConfig(enabled=True)
    fb = git_ops.create_fix_branch(tmp_path / "elsewhere", cfg=cfg, incident_id="inc")
    assert fb is None


def test_create_fix_branch_disabled_via_config(repo: Path):
    cfg = GitConfig(enabled=False)
    assert git_ops.create_fix_branch(repo, cfg=cfg, incident_id="inc") is None


def test_stage_and_commit_all_skips_when_nothing_to_commit(repo: Path):
    out = git_ops.stage_and_commit_all(repo, message="nothing new")
    assert out is None


def test_stage_and_commit_all_returns_hash_on_real_change(repo: Path):
    (repo / "new.txt").write_text("new!\n")
    sha = git_ops.stage_and_commit_all(repo, message="add new.txt")
    assert sha is not None
    assert len(sha) == 40


def test_keep_branch_with_auto_merge_fast_forwards_main(repo: Path):
    cfg = GitConfig(enabled=True, auto_merge=True)
    fb = git_ops.create_fix_branch(repo, cfg=cfg, incident_id="inc-merge")
    assert fb is not None
    (repo / "f.txt").write_text("change\n")
    git_ops.stage_and_commit_all(repo, message="claude fix")
    git_ops.keep_branch(repo, branch=fb, base_branch="main", auto_merge=True)
    # We should be back on main with the change merged.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (repo / "f.txt").exists()


def test_keep_branch_without_auto_merge_leaves_branch(repo: Path):
    cfg = GitConfig(enabled=True, auto_merge=False)
    fb = git_ops.create_fix_branch(repo, cfg=cfg, incident_id="inc-keep")
    assert fb is not None
    (repo / "f.txt").write_text("change\n")
    git_ops.stage_and_commit_all(repo, message="claude fix")
    git_ops.keep_branch(repo, branch=fb, base_branch="main", auto_merge=False)
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    # Branch still exists.
    branches = _git(repo, "branch")
    assert "autosentry/fix-inc-keep" in branches


def test_discard_branch_returns_to_main_with_clean_tree(repo: Path):
    cfg = GitConfig(enabled=True)
    fb = git_ops.create_fix_branch(repo, cfg=cfg, incident_id="inc-bad")
    assert fb is not None
    # Make an uncommitted edit and a stray file.
    (repo / "file.txt").write_text("broken\n")
    (repo / "stray.txt").write_text("stray\n")
    git_ops.discard_branch(repo, branch=fb, base_branch="main")
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    # Working tree is clean — original file.txt content preserved, stray gone.
    assert (repo / "file.txt").read_text() == "hello\n"
    assert not (repo / "stray.txt").exists()
    # Forensic branch is still there.
    assert "autosentry/fix-inc-bad" in _git(repo, "branch")
