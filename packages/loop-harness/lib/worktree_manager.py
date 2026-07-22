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

from loop_common import (
    GIT_TIMEOUT_SECONDS,
    LoopHarnessError,
    RootResolutionError,
    resolve_root_worktree,
)


class WorktreeError(LoopHarnessError):
    """Raised when worktree operations fail."""


@dataclass(frozen=True)
class WorktreeInfo:
    """Loop worktree information."""

    path: str
    branch: str
    repo_identity_hash: str
    # Issue #208 (SEC-H2) hardening, both optional/defaulted for back-compat with existing
    # direct-construction call sites (e.g. test doubles) that predate this field:
    repo_identity_material_digest: str | None = None
    gitlink_fingerprint: str | None = None


def resolve_repo_identity_material(project_dir: str) -> str:
    """Return the raw repository identity material backing `resolve_repo_identity_hash()`.

    Prefers `remote.origin.url`, falling back to the absolute git-common-dir path, and
    finally the resolved `project_dir` itself when neither git query succeeds (e.g. not a
    git repository at all). Split out from `resolve_repo_identity_hash()` (Issue #208) so a
    caller that needs the *full-strength* material -- not the 8-hex-character (32-bit)
    truncated hash, which is only safe for loop_id/state-directory naming, never for security
    verification (see `resolve_repo_identity_material_digest()`) -- does not have to
    re-implement this resolution order.
    """
    material = _git(["config", "--get", "remote.origin.url"], project_dir)
    if not material:
        material = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], project_dir)
    if not material:
        material = str(Path(project_dir).resolve())
    return material


def resolve_repo_identity_hash(project_dir: str) -> str:
    """Return the 8-character repository identity hash."""
    return hashlib.sha256(resolve_repo_identity_material(project_dir).encode("utf-8")).hexdigest()[
        :8
    ]


def resolve_repo_identity_material_digest(project_dir: str) -> str:
    """Return the full (untruncated, 64-hex-character) SHA-256 digest of the identity material.

    Issue #208 (SEC-H2): `resolve_repo_identity_hash()`'s 8-hex-character (32-bit) truncation
    is a realistic target for a crafted-material preimage search once the expected value is
    public -- and it always is, since it is embedded in the loop_id/state-directory name
    (`.claude/loop/<hash>-issue-<N>`). This full digest is meant for the security-sensitive
    re-verification path (`loop_common.is_repo_identity_verified()`) instead, where a 32-bit
    collision budget is not an acceptable safety margin. The 8-hex hash itself is left
    unchanged (still used for loop_id naming) for backward compatibility with existing loop
    state directories.
    """
    return hashlib.sha256(resolve_repo_identity_material(project_dir).encode("utf-8")).hexdigest()


def gitlink_fingerprint(worktree_path: str) -> str | None:
    """Return a SHA-256 digest of a linked worktree's `.git` gitlink pointer, or `None`.

    Issue #208 (SEC-H2): a `git worktree add` checkout has a plain-text `.git` *file* (not a
    directory) containing `gitdir: <path-to-.git/worktrees/<name>>`, which every later
    `git -C worktree_path ...` invocation resolves through to find the shared repository's
    real `.git` directory (config, refs, objects). The Maker has unrestricted `Edit`/`Write`
    access to this file inside the shared worktree; rewriting its content can silently
    redirect every later git operation -- including repo-identity re-verification -- to an
    attacker-controlled decoy repository, with no hash collision required at all (the decoy's
    `remote.origin.url` can simply be set to whatever value the attacker wants it to be, since
    they control the whole decoy repo's config). Pinning this file's content at worktree
    creation time (the earliest trustworthy moment, before any Maker has run) and comparing it
    on every later verification closes that gap.

    Returns `None` when `.git` is missing or is a directory (a non-worktree checkout, e.g. the
    main repository itself) -- there is nothing gitlink-specific to fingerprint in that layout,
    and callers must treat `None` as "not applicable", never as "verified".
    """
    git_path = Path(worktree_path) / ".git"
    if not git_path.is_file():
        return None
    try:
        content = git_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


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
    material_digest = resolve_repo_identity_material_digest(project_dir)
    if is_existing_loop_worktree(path, branch):
        return _worktree_info(path, branch, repo_hash, material_digest)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(project_dir, branch):
        _run_git(["worktree", "add", path, branch], project_dir)
        return _worktree_info(path, branch, repo_hash, material_digest)
    base = base_branch or _default_base_branch(project_dir)
    _run_git(["worktree", "add", "-b", branch, path, base], project_dir)
    return _worktree_info(path, branch, repo_hash, material_digest)


def _worktree_info(path: str, branch: str, repo_hash: str, material_digest: str) -> WorktreeInfo:
    """Build a `WorktreeInfo`, pinning the gitlink fingerprint at this trustworthy moment.

    Issue #208 (SEC-H2): called only from `create_worktree()`'s own return sites, i.e. right
    after the worktree is known to exist and before any Maker has ever had a chance to run --
    the earliest trustworthy moment to read `path`'s `.git` gitlink pointer as a baseline.
    """
    return WorktreeInfo(
        path=path,
        branch=branch,
        repo_identity_hash=repo_hash,
        repo_identity_material_digest=material_digest,
        gitlink_fingerprint=gitlink_fingerprint(path),
    )


def remove_worktree(project_dir: str, issue_number: int, force: bool = False) -> None:
    """Explicitly remove the loop worktree; never called automatically."""
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(worktree_path_for(project_dir, issue_number))
    _run_git(args, project_dir)


def verify_repo_identity(worktree_path: str, expected_hash: str) -> bool:
    """Verify that worktree_path still belongs to the expected repository.

    Unused by the loop-harness driver/step CLIs (they use the hardened
    `loop_common.is_repo_identity_verified()`, Issue #208/SEC-H2, instead); this legacy 8-hex
    truncated-hash comparison is kept only as a standalone utility and is not suitable for
    security-sensitive re-verification against an untrusted worktree.
    """
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


def _branch_exists(project_dir: str, branch: str) -> bool:
    """Return True when a local branch already exists."""
    return bool(_git(["rev-parse", "--verify", f"refs/heads/{branch}"], project_dir))


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
