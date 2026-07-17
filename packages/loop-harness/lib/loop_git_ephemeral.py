#!/usr/bin/env python3
"""Prepare and safely write back Maker-owned ephemeral Git metadata.

This module is deliberately limited to Git plumbing and runtime-directory cleanup. It does
not read or write loop state, journals, or notifications; callers translate the typed results
and failures into the loop protocol at the orchestration boundary.

Internal git-execution, trusted-runtime-validation, and cleanup helpers live in
``loop_git_ephemeral_support.py`` (Codex review, PR #256, Critical -- this module previously
exceeded the 800-line file limit in ``.claude/rules/coding-principles.md``). This is a pure
module split: the public API (``prepare_ephemeral_git`` / ``build_maker_git_mount_spec`` /
``finalize_ephemeral_git`` / ``cleanup_ephemeral_git``) and the typed exceptions
(``EphemeralGitSession`` / ``EphemeralGitSafetyStop`` / ``EphemeralGitInfrastructureError``)
remain importable from this module unchanged; no security boundary or behavior changed as part
of the split.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from loop_git_ephemeral_support import (  # noqa: E402
    EphemeralGitInfrastructureError,
    EphemeralGitSafetyStop,
    GitRunner,
    _attach_cleanup_error,
    _command_failure_details,
    _delete_import_ref,
    _delete_import_ref_best_effort,
    _delete_import_ref_error,
    _ephemeral_env,
    _normalize_branch_ref,
    _restore_ephemeral_git_alternates,
    _restore_ephemeral_git_config,
    _run_git,
    _run_git_unchecked,
    _validate_runtime_location,
    _validate_safe_id,
    _verify_checked_out_branch,
    _verify_git_pointer,
    _verify_worktree_matches_trusted_tree,
)

__all__ = [
    "BindMountSpec",
    "MakerGitMountSpec",
    "EphemeralGitSession",
    "EphemeralGitFinalizeResult",
    "EphemeralGitSafetyStop",
    "EphemeralGitInfrastructureError",
    "prepare_ephemeral_git",
    "build_maker_git_mount_spec",
    "finalize_ephemeral_git",
    "cleanup_ephemeral_git",
]


@dataclass(frozen=True)
class BindMountSpec:
    """One ordered bind mount for a Maker container."""

    source: Path
    target: Path
    read_only: bool


@dataclass(frozen=True)
class MakerGitMountSpec:
    """Ordered mounts and environment needed to expose an ephemeral Git repository."""

    mounts: tuple[BindMountSpec, ...]
    env: Mapping[str, str]


@dataclass(frozen=True)
class EphemeralGitSession:
    """Trusted paths and refs pinned while preparing one Maker action."""

    project_dir: Path
    worktree_path: Path
    common_dir: Path
    runtime_dir: Path
    ephemeral_dir: Path
    pinned_git_pointer: Path
    pinned_git_config: Path
    branch_ref: str
    import_ref: str
    baseline_sha: str


@dataclass(frozen=True)
class EphemeralGitFinalizeResult:
    """Result of comparing and, when needed, importing the ephemeral branch."""

    status: Literal["updated", "no_commit"]
    baseline_sha: str
    new_sha: str


def prepare_ephemeral_git(
    *,
    project_dir: str | Path,
    loop_id: str,
    action_id: str,
    worktree_path: str | Path,
    branch: str,
    runner: GitRunner = subprocess.run,
) -> EphemeralGitSession:
    """Create one action-scoped bare GIT_DIR with a baseline-initialized index."""
    try:
        return _prepare_ephemeral_git(
            project_dir=project_dir,
            loop_id=loop_id,
            action_id=action_id,
            worktree_path=worktree_path,
            branch=branch,
            runner=runner,
        )
    except (ValueError, EphemeralGitSafetyStop, EphemeralGitInfrastructureError):
        raise
    except OSError as exc:
        raise EphemeralGitInfrastructureError(
            "filesystem failure before preparing ephemeral git"
        ) from exc


def _prepare_ephemeral_git(
    *,
    project_dir: str | Path,
    loop_id: str,
    action_id: str,
    worktree_path: str | Path,
    branch: str,
    runner: GitRunner,
) -> EphemeralGitSession:
    _validate_safe_id("loop_id", loop_id)
    _validate_safe_id("action_id", action_id)
    branch_ref = _normalize_branch_ref(branch, runner=runner)

    requested_project = Path(project_dir).resolve()
    worktree = Path(worktree_path).resolve()
    import_ref = f"refs/loop-import/{action_id}"

    common_result = _run_git(
        ["-C", worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        runner=runner,
        operation="resolve git common directory",
    )
    common_dir = Path(common_result.stdout.strip())
    if not common_dir.is_absolute() or not common_dir.is_dir():
        raise EphemeralGitInfrastructureError(
            "resolved git common directory is not an absolute directory",
            details={"common_dir": str(common_dir)},
        )
    common_dir = common_dir.resolve()

    project_common_result = _run_git(
        ["-C", requested_project, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        runner=runner,
        operation="resolve project git common directory",
    )
    project_common_dir = Path(project_common_result.stdout.strip()).resolve()
    if project_common_dir != common_dir:
        raise EphemeralGitInfrastructureError(
            "project and worktree do not share the same git common directory",
            details={
                "project_common_dir": str(project_common_dir),
                "worktree_common_dir": str(common_dir),
            },
        )

    project = common_dir.parent.resolve()
    runtime_dir = project / ".claude" / "loop" / loop_id / "docker-runtime" / action_id
    ephemeral_dir = runtime_dir / "git-ephemeral"
    pinned_git_pointer = runtime_dir / "pinned-dotgit"
    pinned_git_config = runtime_dir / "pinned-git-config"

    baseline_result = _run_git(
        ["-C", worktree, "rev-parse", "--verify", branch_ref],
        runner=runner,
        operation="resolve baseline branch",
    )
    baseline_sha = baseline_result.stdout.strip()
    if not baseline_sha:
        raise EphemeralGitInfrastructureError("baseline branch resolved to an empty object id")

    _verify_checked_out_branch(
        worktree,
        branch_ref,
        runner=runner,
        operation="verify checked-out branch before preparing ephemeral git",
    )

    _validate_runtime_location(project, worktree, runtime_dir)
    _remove_runtime(runtime_dir)
    try:
        runtime_dir.mkdir(parents=True, mode=0o700)
    except OSError as exc:
        raise EphemeralGitInfrastructureError(
            "failed to create the ephemeral git runtime",
            details={"runtime_dir": str(runtime_dir)},
        ) from exc
    _validate_runtime_location(project, worktree, runtime_dir)

    session = EphemeralGitSession(
        project_dir=project,
        worktree_path=worktree,
        common_dir=common_dir,
        runtime_dir=runtime_dir,
        ephemeral_dir=ephemeral_dir,
        pinned_git_pointer=pinned_git_pointer,
        pinned_git_config=pinned_git_config,
        branch_ref=branch_ref,
        import_ref=import_ref,
        baseline_sha=baseline_sha,
    )

    try:
        # Fix-10 (PR #256 review, High; design doc §4.3.1 step 11): reject a worktree that
        # already has uncommitted changes relative to baseline *before* the Maker-writable
        # ephemeral GIT_DIR is created. worktree_manager.create_worktree() may reuse a worktree
        # left over from a previous, interrupted action; without this check, that leftover
        # content would silently seed the new ephemeral index and later pass finalize's own
        # trusted-tree check as part of a candidate commit. Uses the same host-only,
        # Maker-unreachable trusted-index comparison as finalize's Fix-9 check, against
        # `common_dir` (the real, already-trusted repository) rather than `ephemeral_dir`
        # (which does not exist yet at this point in prepare).
        _verify_worktree_matches_trusted_tree(
            git_dir=common_dir,
            worktree_path=worktree,
            target_sha=baseline_sha,
            temp_index_dir=runtime_dir,
            runner=runner,
        )
        _delete_import_ref(session, runner=runner)
        _run_git(
            ["init", "--bare", ephemeral_dir],
            runner=runner,
            operation="initialize ephemeral git directory",
        )
        _run_git(
            ["--git-dir", ephemeral_dir, "config", "user.name", "loop-harness-maker"],
            runner=runner,
            operation="seed ephemeral git user name",
        )
        _run_git(
            [
                "--git-dir",
                ephemeral_dir,
                "config",
                "user.email",
                "loop-harness-maker@invalid",
            ],
            runner=runner,
            operation="seed ephemeral git user email",
        )

        git_config = ephemeral_dir / "config"
        if git_config.is_symlink() or not git_config.is_file():
            raise EphemeralGitInfrastructureError(
                "ephemeral git config is not a trusted regular file",
                details={"git_config": str(git_config)},
            )
        pinned_git_config.write_bytes(git_config.read_bytes())
        pinned_git_config.chmod(0o400)

        alternates = ephemeral_dir / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(f"{common_dir / 'objects'}\n", encoding="utf-8")

        _run_git(
            ["--git-dir", ephemeral_dir, "update-ref", branch_ref, baseline_sha],
            runner=runner,
            operation="seed ephemeral branch",
        )
        _run_git(
            ["--git-dir", ephemeral_dir, "symbolic-ref", "HEAD", branch_ref],
            runner=runner,
            operation="seed ephemeral HEAD",
        )
        _run_git(
            ["read-tree", baseline_sha],
            runner=runner,
            env=_ephemeral_env(session),
            operation="initialize ephemeral index",
        )

        git_pointer = worktree / ".git"
        if git_pointer.is_symlink() or not git_pointer.is_file():
            raise EphemeralGitInfrastructureError(
                "worktree .git pointer is not a trusted regular file",
                details={"git_pointer": str(git_pointer)},
            )
        pinned_git_pointer.write_bytes(git_pointer.read_bytes())
        pinned_git_pointer.chmod(0o444)
        return session
    except (EphemeralGitInfrastructureError, EphemeralGitSafetyStop):
        _delete_import_ref_best_effort(session, runner=runner)
        _remove_runtime_best_effort(runtime_dir)
        raise
    except OSError as exc:
        _delete_import_ref_best_effort(session, runner=runner)
        _remove_runtime_best_effort(runtime_dir)
        raise EphemeralGitInfrastructureError(
            "filesystem failure while preparing ephemeral git",
            details={"runtime_dir": str(runtime_dir)},
        ) from exc


def build_maker_git_mount_spec(session: EphemeralGitSession) -> MakerGitMountSpec:
    """Return ordered 1:1 mounts; the .git file overlay must follow the worktree mount.

    `mounts` is an ordered tuple, not a set: the `pinned_git_pointer` -> `<worktree>/.git` ro
    mount must be applied *after* the rw worktree mount, so the more specific `.git` mount wins
    and only that single file ends up read-only (see the design doc §4.3.2 note this mirrors).
    The Docker backend that wires this spec into an actual `docker run`/mount invocation (Phase 4,
    currently unimplemented -- `execution_backend` stays `none` through Phase 2) MUST preserve
    this ordering when translating `mounts` into `-v`/bind-mount flags; reordering (e.g. sorting
    mounts by path) would silently drop the `.git` write protection.
    """
    return MakerGitMountSpec(
        mounts=(
            BindMountSpec(session.worktree_path, session.worktree_path, False),
            BindMountSpec(
                session.pinned_git_pointer,
                session.worktree_path / ".git",
                True,
            ),
            BindMountSpec(session.ephemeral_dir, session.ephemeral_dir, False),
            BindMountSpec(
                session.common_dir / "objects",
                session.common_dir / "objects",
                True,
            ),
        ),
        env={
            "GIT_DIR": str(session.ephemeral_dir),
            "GIT_WORK_TREE": str(session.worktree_path),
        },
    )


def finalize_ephemeral_git(
    session: EphemeralGitSession,
    *,
    runner: GitRunner = subprocess.run,
) -> EphemeralGitFinalizeResult:
    """Import one Maker commit chain and atomically fast-forward the shared branch.

    Every exit removes this action's temporary import ref. A successful CAS followed by a
    failed worktree reset is reported as infrastructure failure without rolling the ref back.
    """
    primary_error: BaseException | None = None
    result: EphemeralGitFinalizeResult | None = None
    try:
        _delete_import_ref(session, runner=runner)
        result = _finalize_ephemeral_git(session, runner=runner)
    except BaseException as exc:
        primary_error = exc

    cleanup_error = _delete_import_ref_error(session, runner=runner)
    if primary_error is not None:
        if cleanup_error is not None:
            _attach_cleanup_error(primary_error, cleanup_error)
        raise primary_error
    if cleanup_error is not None:
        raise cleanup_error
    if result is None:  # pragma: no cover - defensive invariant
        raise EphemeralGitInfrastructureError("ephemeral git finalize produced no result")
    return result


def cleanup_ephemeral_git(
    session: EphemeralGitSession,
    *,
    runner: GitRunner = subprocess.run,
) -> None:
    """Idempotently remove one action's temporary import ref and local runtime."""
    ref_error = _delete_import_ref_error(session, runner=runner)
    runtime_error: EphemeralGitInfrastructureError | None = None
    try:
        _remove_runtime(session.runtime_dir)
    except EphemeralGitInfrastructureError as exc:
        runtime_error = exc
    if ref_error is not None:
        if runtime_error is not None:
            _attach_cleanup_error(ref_error, runtime_error)
        raise ref_error
    if runtime_error is not None:
        raise runtime_error


def _finalize_ephemeral_git(
    session: EphemeralGitSession,
    *,
    runner: GitRunner,
) -> EphemeralGitFinalizeResult:
    _restore_ephemeral_git_config(session)
    _restore_ephemeral_git_alternates(session)
    new_sha_result = _run_git(
        ["--git-dir", session.ephemeral_dir, "rev-parse", "--verify", session.branch_ref],
        runner=runner,
        operation="resolve ephemeral branch",
    )
    new_sha = new_sha_result.stdout.strip()

    _verify_git_pointer(session)
    _verify_checked_out_branch(
        session.worktree_path,
        session.branch_ref,
        runner=runner,
        operation="verify checked-out branch before ephemeral status",
    )
    _verify_worktree_matches_trusted_tree(
        git_dir=session.ephemeral_dir,
        worktree_path=session.worktree_path,
        target_sha=new_sha,
        temp_index_dir=session.runtime_dir,
        runner=runner,
    )

    if new_sha == session.baseline_sha:
        return EphemeralGitFinalizeResult(
            status="no_commit",
            baseline_sha=session.baseline_sha,
            new_sha=new_sha,
        )

    fetch_result = _run_git_unchecked(
        [
            "--git-dir",
            session.common_dir,
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            session.ephemeral_dir,
            f"{session.branch_ref}:{session.import_ref}",
        ],
        runner=runner,
        operation="fetch ephemeral branch into temporary import ref",
    )
    if fetch_result.returncode != 0:
        raise EphemeralGitSafetyStop(
            "git_ref_import_failed",
            "failed to import the ephemeral branch into its temporary ref",
            details=_command_failure_details(fetch_result),
        )

    imported_result = _run_git(
        ["--git-dir", session.common_dir, "rev-parse", "--verify", session.import_ref],
        runner=runner,
        operation="resolve temporary import ref",
    )
    imported_sha = imported_result.stdout.strip()
    if imported_sha != new_sha:
        raise EphemeralGitSafetyStop(
            "git_ref_import_failed",
            "temporary import ref does not match the pinned ephemeral branch tip",
            details={"ephemeral_sha": new_sha, "imported_sha": imported_sha},
        )

    merge_base = _run_git_unchecked(
        [
            "--git-dir",
            session.common_dir,
            "merge-base",
            "--is-ancestor",
            session.baseline_sha,
            imported_sha,
        ],
        runner=runner,
        operation="verify fast-forward ancestry",
    )
    if merge_base.returncode == 1:
        raise EphemeralGitSafetyStop(
            "git_ref_not_fast_forward",
            "ephemeral branch is not a fast-forward of the baseline",
            details={
                "baseline_sha": session.baseline_sha,
                "candidate_sha": imported_sha,
            },
        )
    if merge_base.returncode != 0:
        raise EphemeralGitInfrastructureError(
            "git merge-base failed while checking fast-forward ancestry",
            details=_command_failure_details(merge_base),
        )

    _verify_git_pointer(session)
    _verify_checked_out_branch(
        session.worktree_path,
        session.branch_ref,
        runner=runner,
        operation="verify checked-out branch before shared branch update",
    )
    cas_result = _run_git_unchecked(
        [
            "--git-dir",
            session.common_dir,
            "update-ref",
            session.branch_ref,
            imported_sha,
            session.baseline_sha,
        ],
        runner=runner,
        operation="compare-and-swap shared branch",
    )
    if cas_result.returncode != 0:
        raise EphemeralGitSafetyStop(
            "git_ref_cas_rejected",
            "shared branch changed before the compare-and-swap update",
            details={
                "baseline_sha": session.baseline_sha,
                "candidate_sha": imported_sha,
                **_command_failure_details(cas_result),
            },
        )

    reset_result = _run_git_unchecked(
        ["-C", session.worktree_path, "reset", "--mixed", "HEAD"],
        runner=runner,
        operation="reset linked-worktree index after branch update",
    )
    if reset_result.returncode != 0:
        raise EphemeralGitInfrastructureError(
            "post-CAS worktree reset failed; the branch update was not rolled back",
            details={
                "baseline_sha": session.baseline_sha,
                "new_sha": imported_sha,
                **_command_failure_details(reset_result),
            },
        )
    _verify_checked_out_branch(
        session.worktree_path,
        session.branch_ref,
        runner=runner,
        operation="verify checked-out branch after worktree reset",
    )

    return EphemeralGitFinalizeResult(
        status="updated",
        baseline_sha=session.baseline_sha,
        new_sha=imported_sha,
    )


def _remove_runtime(runtime_dir: Path) -> None:
    try:
        if runtime_dir.is_symlink() or runtime_dir.is_file():
            runtime_dir.unlink(missing_ok=True)
        elif runtime_dir.exists():
            shutil.rmtree(runtime_dir)
    except OSError as exc:
        raise EphemeralGitInfrastructureError(
            "failed to remove the ephemeral git runtime",
            details={"runtime_dir": str(runtime_dir)},
        ) from exc


def _remove_runtime_best_effort(runtime_dir: Path) -> None:
    try:
        _remove_runtime(runtime_dir)
    except EphemeralGitInfrastructureError:
        pass
