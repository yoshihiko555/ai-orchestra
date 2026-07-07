#!/usr/bin/env python3
"""Worktree helpers for loop-harness."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from loop_common import GIT_TIMEOUT_SECONDS, RootResolutionError, resolve_root_worktree


class WorktreeError(RuntimeError):
    """Raised when worktree operations fail."""


@dataclass(frozen=True)
class WorktreeInfo:
    """Loop worktree information."""

    path: str
    branch: str
    repo_identity_hash: str


def resolve_repo_identity_hash(project_dir: str) -> str:
    """Return the 8-character repository identity hash."""
    material = _git(["config", "--get", "remote.origin.url"], project_dir)
    if not material:
        material = _git(["rev-parse", "--show-toplevel"], project_dir)
    if not material:
        material = str(Path(project_dir).resolve())
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


def compute_loop_id(project_dir: str, issue_number: int) -> str:
    """Return <hash8>-issue-<N>."""
    return f"{resolve_repo_identity_hash(project_dir)}-issue-{issue_number}"


def branch_name_for(issue_number: int) -> str:
    """Return the loop branch name for an issue."""
    return f"loop/issue-{issue_number}"


def worktree_path_for(project_dir: str, issue_number: int) -> str:
    """Return <root>/.worktrees/loop-issue-<N>."""
    root = _root_worktree(project_dir)
    return str(root / ".worktrees" / f"loop-issue-{issue_number}")


def is_existing_loop_worktree(worktree_path: str, expected_branch: str) -> bool:
    """Return True when path is an existing independent worktree on expected branch."""
    if not os.path.isdir(worktree_path):
        return False
    git_dir = _git(["rev-parse", "--git-dir"], worktree_path)
    common_dir = _git(["rev-parse", "--git-common-dir"], worktree_path)
    is_worktree = bool(git_dir) and bool(common_dir) and git_dir != common_dir
    current_branch = _git(["branch", "--show-current"], worktree_path)
    return is_worktree and current_branch == expected_branch


def create_worktree(
    project_dir: str, issue_number: int, base_branch: str | None = None
) -> WorktreeInfo:
    """Create or reuse the loop worktree for an issue."""
    branch = branch_name_for(issue_number)
    path = worktree_path_for(project_dir, issue_number)
    repo_hash = resolve_repo_identity_hash(project_dir)
    if is_existing_loop_worktree(path, branch):
        return WorktreeInfo(path=path, branch=branch, repo_identity_hash=repo_hash)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    base = base_branch or _default_base_branch(project_dir)
    _run_git(["worktree", "add", "-b", branch, path, base], project_dir)
    return WorktreeInfo(path=path, branch=branch, repo_identity_hash=repo_hash)


def remove_worktree(project_dir: str, issue_number: int, force: bool = False) -> None:
    """Explicitly remove the loop worktree; never called automatically."""
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(worktree_path_for(project_dir, issue_number))
    _run_git(args, project_dir)


def verify_repo_identity(worktree_path: str, expected_hash: str) -> bool:
    """Verify that worktree_path still belongs to the expected repository."""
    return resolve_repo_identity_hash(worktree_path) == expected_hash


def _default_base_branch(project_dir: str) -> str:
    """Resolve a safe default base branch."""
    ref = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], project_dir)
    if ref.startswith("refs/remotes/origin/"):
        return ref.removeprefix("refs/remotes/origin/")
    for candidate in ("origin/main", "main", "master"):
        if _git(["rev-parse", "--verify", candidate], project_dir):
            return candidate
    raise WorktreeError("could not resolve base branch")


def _root_worktree(project_dir: str) -> Path:
    """Resolve root worktree or convert root resolution errors to WorktreeError."""
    try:
        return resolve_root_worktree(project_dir)
    except RootResolutionError as exc:
        raise WorktreeError(str(exc)) from exc


def _git(args: list[str], cwd: str) -> str:
    """Run git and return stdout, or an empty string on failure."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _run_git(args: list[str], cwd: str) -> None:
    """Run git and raise WorktreeError on failure."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"git {' '.join(args)} failed") from exc
    if completed.returncode != 0:
        raise WorktreeError(completed.stderr.strip() or f"git {' '.join(args)} failed")
