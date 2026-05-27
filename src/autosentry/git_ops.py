"""Thin git wrapper for fix-branch isolation.

Borrowed shape: ``autoresearch`` runs each experiment on a dedicated
branch and keeps/discards by manipulating git. We do the same for
healer fix attempts.

Every function here is a no-op (with a logged warning) when the cwd
isn't a git repo or when ``GitConfig.enabled`` / ``write_enabled`` is
false. The caller never has to wrap calls in try/except.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from autosentry.config import GitConfig
from autosentry.logger import log


@dataclass(frozen=True)
class GitContext:
    """The git state at the start of a fix attempt."""

    repo_root: Path
    base_branch: str
    base_commit: str


@dataclass(frozen=True)
class FixBranch:
    """A branch the healer created to land its edits on."""

    name: str
    base_commit: str


# ----- discovery -----------------------------------------------------------


def is_git_repo(cwd: Path) -> bool:
    """``True`` if ``cwd`` (or any ancestor) is a git working tree."""
    return _run_text(["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"]) == "true"


def context(cwd: Path) -> GitContext | None:
    """Snapshot the current branch + commit. Returns ``None`` outside a repo."""
    if not is_git_repo(cwd):
        return None
    root = _run_text(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"])
    branch = _run_text(["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"])
    commit = _run_text(["git", "-C", str(cwd), "rev-parse", "HEAD"])
    if not root or not branch or not commit:
        return None
    return GitContext(repo_root=Path(root), base_branch=branch, base_commit=commit)


# ----- branch lifecycle ----------------------------------------------------


def create_fix_branch(cwd: Path, *, cfg: GitConfig, incident_id: str) -> FixBranch | None:
    """Create a fresh ``<prefix><incident_id>`` branch off the current HEAD.

    Returns ``None`` when git isn't usable (no repo, disabled in config,
    or write_enabled false). Logs the reason.
    """
    if not cfg.enabled or not cfg.write_enabled:
        return None
    ctx = context(cwd)
    if ctx is None:
        log().info("git: not a repo — skipping fix-branch creation")
        return None
    name = f"{cfg.branch_prefix}{incident_id}"
    # If a branch by this name somehow already exists, suffix it. Should be
    # rare — incident ids include a timestamp.
    if _branch_exists(cwd, name):
        suffix = 1
        while _branch_exists(cwd, f"{name}-{suffix}"):
            suffix += 1
        name = f"{name}-{suffix}"
    rc, out, err = _run(
        [
            "git",
            "-C",
            str(cwd),
            "switch",
            "-c",
            name,
        ]
    )
    if rc != 0:
        log().error(f"git: switch -c {name} failed: {err.strip()}")
        return None
    log().action(f"git: created fix branch {name}")
    return FixBranch(name=name, base_commit=ctx.base_commit)


def stage_and_commit_all(cwd: Path, *, message: str, allow_empty: bool = False) -> str | None:
    """Stage every change in the working tree and commit. Returns the
    commit hash or ``None`` if there was nothing to commit (and
    ``allow_empty`` is false).
    """
    _run(["git", "-C", str(cwd), "add", "-A"])
    if not allow_empty:
        rc, out, _ = _run(["git", "-C", str(cwd), "status", "--porcelain"])
        if rc == 0 and not out.strip():
            return None
    # ``--no-gpg-sign`` keeps autosentry's automated commits from prompting
    # the user's signer (1Password etc.) and stalling/failing under
    # supervision. The user can always sign the final merge commit by hand.
    cmd = ["git", "-C", str(cwd), "commit", "--no-gpg-sign", "-m", message]
    if allow_empty:
        cmd.insert(-2, "--allow-empty")
    rc, _, err = _run(cmd)
    if rc != 0:
        log().error(f"git: commit failed: {err.strip()}")
        return None
    return _run_text(["git", "-C", str(cwd), "rev-parse", "HEAD"])


def keep_branch(cwd: Path, *, branch: FixBranch, base_branch: str, auto_merge: bool) -> None:
    """Mark a verified fix as kept.

    If ``auto_merge`` is true, fast-forward ``base_branch`` into the fix
    branch's tip and delete the fix branch. Otherwise, return to
    ``base_branch`` and leave the fix branch behind for manual review.
    """
    if auto_merge:
        # Switch back to base, then fast-forward.
        rc, _, err = _run(["git", "-C", str(cwd), "switch", base_branch])
        if rc != 0:
            log().error(f"git: switch {base_branch} failed: {err.strip()}")
            return
        rc, _, err = _run(["git", "-C", str(cwd), "merge", "--ff-only", branch.name])
        if rc != 0:
            log().error(f"git: ff-merge {branch.name} into {base_branch} failed: {err.strip()}")
            return
        _run(["git", "-C", str(cwd), "branch", "-d", branch.name])
        log().action(f"git: kept fix — merged {branch.name} into {base_branch}, deleted branch")
        return
    # Switch back to base; leave the branch standing.
    rc, _, err = _run(["git", "-C", str(cwd), "switch", base_branch])
    if rc != 0:
        log().error(f"git: switch {base_branch} failed: {err.strip()}")
        return
    log().action(
        f"git: kept fix on branch {branch.name} (auto_merge off) — operator merges by hand"
    )


def discard_branch(cwd: Path, *, branch: FixBranch, base_branch: str) -> None:
    """Regression / crash path: return to ``base_branch`` with a clean
    working tree. The branch is kept as a forensic artifact.
    """
    # Drop any in-progress edits BEFORE switching to avoid carrying them.
    _run(["git", "-C", str(cwd), "restore", "."])
    _run(["git", "-C", str(cwd), "clean", "-fd"])
    rc, _, err = _run(["git", "-C", str(cwd), "switch", base_branch])
    if rc != 0:
        log().error(f"git: switch {base_branch} after revert failed: {err.strip()}")
        return
    log().action(
        f"git: discarded fix — back on {base_branch}, branch {branch.name} kept for review"
    )


# ----- helpers -------------------------------------------------------------


def _branch_exists(cwd: Path, name: str) -> bool:
    rc, _, _ = _run(
        [
            "git",
            "-C",
            str(cwd),
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{name}",
        ]
    )
    return rc == 0


def _run(cmd: list[str]) -> tuple[int, str, str]:
    """Run a command; return (returncode, stdout, stderr). No raise."""
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        return 127, "", str(e)
    return result.returncode, result.stdout, result.stderr


def _run_text(cmd: list[str]) -> str:
    rc, out, _ = _run(cmd)
    if rc != 0:
        return ""
    return out.strip()
