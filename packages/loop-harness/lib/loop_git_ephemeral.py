#!/usr/bin/env python3
"""Prepare and safely write back Maker-owned ephemeral Git metadata.

This module is deliberately limited to Git plumbing and runtime-directory cleanup. It does
not read or write loop state, journals, or notifications; callers translate the typed results
and failures into the loop protocol at the orchestration boundary.

Internal git-execution, trusted-runtime-validation, and cleanup helpers live in
``loop_git_ephemeral_support.py`` (Codex review, PR #256, Critical -- this module previously
exceeded the 800-line file limit in ``.claude/rules/coding-principles.md``). This is a pure
module split: the public API (``prepare_ephemeral_git`` / ``build_maker_git_mount_spec`` /
``build_checker_git_mount_spec`` / ``finalize_ephemeral_git`` / ``cleanup_ephemeral_git``) and the typed exceptions
(``EphemeralGitSession`` / ``EphemeralGitSafetyStop`` / ``EphemeralGitInfrastructureError``)
remain importable from this module unchanged; no security boundary or behavior changed as part
of the split.
"""

from __future__ import annotations

import os
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
    _harden_ephemeral_git_metadata,
    _normalize_branch_ref,
    _run_git,
    _run_git_unchecked,
    _validate_common_objects_mount_source,
    _validate_runtime_location,
    _validate_safe_id,
    _verify_checked_out_branch,
    _verify_checker_baseline_matches_branch_tip,
    _verify_git_pointer,
    _verify_worktree_matches_trusted_tree,
)
from loop_local_override_guard import (  # noqa: E402
    LocalOverrideSnapshot,
    LocalOverrideSnapshotError,
    changed_local_override_paths,
    snapshot_local_overrides,
)

__all__ = [
    "BindMountSpec",
    "CheckerGitMountSpec",
    "MakerGitMountSpec",
    "EphemeralGitSession",
    "EphemeralGitFinalizeResult",
    "EphemeralGitSafetyStop",
    "EphemeralGitInfrastructureError",
    "prepare_ephemeral_git",
    "build_checker_git_mount_spec",
    "build_maker_git_mount_spec",
    "verify_failed_maker_worktree",
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
class CheckerGitMountSpec:
    """Ordered read-only mounts and environment for one Checker container."""

    mounts: tuple[BindMountSpec, ...]
    env: Mapping[str, str]


@dataclass(frozen=True)
class EphemeralGitSession:
    """Trusted paths and refs pinned while preparing one Maker action.

    Also used, unmodified, to build the Checker's own sanitized read-only mount spec
    (``build_checker_git_mount_spec``, design doc §4.3.4): a Checker run gets its own session,
    prepared fresh with ``baseline_sha`` pinned to the branch tip at Checker execution time --
    it never reuses a Maker session still in flight. ``build_checker_git_mount_spec`` enforces
    that freshness at runtime via ``_verify_checker_baseline_matches_branch_tip``.
    """

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
    local_override_snapshot: tuple[LocalOverrideSnapshot, ...]


@dataclass(frozen=True)
class EphemeralGitFinalizeResult:
    """Result of comparing and, when needed, importing the ephemeral branch."""

    status: Literal["updated", "no_commit"]
    baseline_sha: str
    new_sha: str


# Codex review, PR #262, High (round 7): the exact message `_verify_worktree_matches_trusted_tree`
# raises for leftover worktree drift (as opposed to any of its other, genuinely-infrastructure
# failure modes). Shared by `verify_failed_maker_worktree()` (failed-Maker path) and
# `_finalize_ephemeral_git()`'s pre-CAS check (successful-Maker path, see
# `_as_partial_worktree_stop()`) so both convert only this specific drift signal into the
# documented `maker_partial_worktree` safe-stop, not every unrelated infrastructure error.
_DIRTY_WORKTREE_MESSAGE = "worktree status is dirty relative to the trusted target tree"


def _as_partial_worktree_stop(
    exc: EphemeralGitInfrastructureError,
) -> EphemeralGitInfrastructureError:
    """Re-raise a dirty-worktree drift signal as `maker_partial_worktree`, or pass through.

    Codex review, PR #262, High (round 7): `_finalize_ephemeral_git()`'s pre-CAS trusted-tree
    check runs before any shared-branch mutation, so a dirty verdict there is exactly the same
    safe, no-side-effect situation `verify_failed_maker_worktree()` already reports as
    `maker_partial_worktree` for a failed Maker -- a successful (exit 0) Maker that still left
    uncommitted worktree changes deserves the identical non-destructive safe-stop, not an opaque
    Docker infrastructure failure that discards the sealed result and blocks the later `abort()`
    path (see `loop_docker_action.DockerActionRuntime._finish_git()`).
    """
    if str(exc) != _DIRTY_WORKTREE_MESSAGE:
        return exc
    return EphemeralGitSafetyStop(
        "maker_partial_worktree",
        "Maker left uncommitted worktree changes",
        details=exc.details,
    )


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

    # Codex review, PR #256, High: `common_dir.parent` is only the caller's project_dir for the
    # common "$GIT_DIR is a direct child of the worktree root" layout. Under
    # `git init --separate-git-dir=<elsewhere>` (or an equivalent relocated common Git dir), the
    # common dir's parent can be an arbitrary directory outside the caller's project entirely --
    # putting the runtime dir (and the pinned snapshots inside it) there instead of inside the
    # project the caller actually asked for. `requested_project` was already verified above to
    # share the same git-common-dir as `worktree`, so it is the trusted basis for the runtime
    # location; `_validate_runtime_location` still enforces that the runtime stays outside the
    # worktree itself.
    project = requested_project
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
    # CodeRabbit review, PR #262, Medium: snapshot local overrides before the runtime
    # directory is created (rather than after) so a read failure here never leaves a
    # freshly created runtime_dir behind. This snapshot call is outside the cleanup
    # `try` below by design -- moving it earlier means there is nothing to clean up yet.
    local_override_snapshot = _snapshot_local_overrides_or_raise(worktree)
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
        local_override_snapshot=local_override_snapshot,
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
        # Codex review, PR #256, High: `git init --bare` with no --object-format defaults to
        # sha1 regardless of the source repository's actual object format. For a SHA-256 source
        # repo this creates a sha1 ephemeral repo whose 40-hex-digit object model rejects the
        # 64-hex-digit baseline SHA the very next `update-ref` below tries to seed, breaking
        # every SHA-256 repository outright. Detecting and propagating the source format keeps
        # the ephemeral repo's object model identical to the repository it mirrors.
        object_format_result = _run_git_unchecked(
            ["-C", worktree, "rev-parse", "--show-object-format"],
            runner=runner,
            operation="detect source object format",
        )
        init_args: list[object] = ["init", "--bare"]
        if object_format_result.returncode == 0 and object_format_result.stdout.strip():
            init_args.append(f"--object-format={object_format_result.stdout.strip()}")
        init_args.append(ephemeral_dir)
        _run_git(
            init_args,
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
        _run_git(
            [
                "--git-dir",
                ephemeral_dir,
                "config",
                "safe.directory",
                str(worktree),
            ],
            runner=runner,
            operation="seed the trusted container worktree path",
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
        _verify_local_override_snapshot(session, safety_stop=False)
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
    common_objects = _validate_common_objects_mount_source(session)
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
                common_objects,
                common_objects,
                True,
            ),
        ),
        env={
            "GIT_DIR": str(session.ephemeral_dir),
            "GIT_WORK_TREE": str(session.worktree_path),
        },
    )


def build_checker_git_mount_spec(
    session: EphemeralGitSession,
    *,
    runner: GitRunner = subprocess.run,
) -> CheckerGitMountSpec:
    """Return the sanitized, ordered read-only mounts for one Checker container.

    The worktree mount must be applied before the more specific pinned ``.git`` overlay. Phase 4's
    Docker backend MUST preserve this tuple order when translating it into bind-mount flags; path
    sorting would silently discard the overlay guarantee. It must also rebuild or revalidate this
    spec immediately before mounting so a changed bind source fails closed. Only
    ``common_dir/objects`` is exposed from shared Git metadata -- refs, config, hooks, and reflogs
    are never mounted.

    Checker mounts the ephemeral directory read-only, but the trusted config and alternates are
    still restored immediately before the spec crosses the container boundary. This deliberately
    reuses Phase 2's recursive objects-symlink rejection and alternates overwrite rather than
    creating a weaker Checker-only preparation path.

    Phase 3 review (Medium x2): this used to only re-validate the shared *objects* mount source
    (``_validate_common_objects_mount_source`` / ``_harden_ephemeral_git_metadata``'s alternates
    and symlink rejection), leaving two gaps ``finalize_ephemeral_git`` already closes for Maker.
    Both are now enforced here too, in the same place Maker's finalize re-checks them:

    - ``_verify_checker_baseline_matches_branch_tip`` asserts ``session.baseline_sha`` is still
      the live tip of ``session.branch_ref`` in the trusted ``common_dir``, i.e. this really is a
      session freshly prepared for *this* Checker run (design doc §4.3.4 step 1) rather than a
      Maker session still in flight or one left over from an earlier action. Codex review (PR
      #258, High): a Maker session that committed into its own ``ephemeral_dir`` but has not yet
      finalized leaves the shared ``common_dir`` branch untouched at ``baseline_sha``, so that tip
      comparison alone does not catch it. ``_verify_checker_baseline_matches_branch_tip`` now also
      rejects such a session directly, by checking that its own ephemeral branch ref has not
      advanced past ``baseline_sha`` and that its worktree still matches the ``baseline_sha`` tree
      (an uncommitted Maker edit) -- see that function's docstring for the full rationale.
    - ``_verify_git_pointer`` re-checks the worktree's ``.git`` pointer file against its pinned
      snapshot. Docker bind mounts follow a symlink source to its resolved target, so a
      Maker-writable worktree whose ``.git`` had been swapped for a symlink before this spec is
      built would otherwise silently bind-mount whatever that symlink resolves to instead of the
      pinned, read-only ``.git`` overlay.

    ``_validate_common_objects_mount_source`` runs first, ahead of
    ``_verify_checker_baseline_matches_branch_tip``: the latter's worktree-drift check (PR #258)
    reads ``baseline_sha``'s tree through ``common_dir``, so ``common_dir/objects`` must already be
    confirmed to be a real, untampered directory or that read fails with a confusing, unrelated
    error instead of the intended "shared git objects mount source is not a trusted directory".
    """
    common_objects = _validate_common_objects_mount_source(session)
    _verify_checker_baseline_matches_branch_tip(session, runner=runner)
    _harden_ephemeral_git_metadata(session)
    _verify_git_pointer(session)
    return CheckerGitMountSpec(
        mounts=(
            BindMountSpec(session.worktree_path, session.worktree_path, True),
            BindMountSpec(
                session.pinned_git_pointer,
                session.worktree_path / ".git",
                True,
            ),
            BindMountSpec(session.ephemeral_dir, session.ephemeral_dir, True),
            BindMountSpec(
                common_objects,
                common_objects,
                True,
            ),
        ),
        env={
            "GIT_DIR": str(session.ephemeral_dir),
            "GIT_WORK_TREE": str(session.worktree_path),
        },
    )


def verify_failed_maker_worktree(
    session: EphemeralGitSession,
    *,
    runner: GitRunner = subprocess.run,
) -> None:
    """Safe-stop when a failed Maker left worktree changes; never reset or clean them."""
    _verify_local_override_snapshot(session, safety_stop=True)
    try:
        _verify_worktree_matches_trusted_tree(
            git_dir=session.common_dir,
            worktree_path=session.worktree_path,
            target_sha=session.baseline_sha,
            temp_index_dir=session.runtime_dir,
            runner=runner,
        )
    except EphemeralGitInfrastructureError as exc:
        converted = _as_partial_worktree_stop(exc)
        if converted is exc:
            raise
        raise converted from exc


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
    _verify_local_override_snapshot(session, safety_stop=True)
    _harden_ephemeral_git_metadata(session)
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
    try:
        _verify_worktree_matches_trusted_tree(
            git_dir=session.ephemeral_dir,
            worktree_path=session.worktree_path,
            target_sha=new_sha,
            temp_index_dir=session.runtime_dir,
            runner=runner,
        )
    except EphemeralGitInfrastructureError as exc:
        # Codex review, PR #262, High (round 7): this check runs before any shared-branch
        # mutation below (fetch/CAS/reset), so a dirty verdict here is exactly the same safe,
        # no-side-effect "Maker left worktree drift behind" situation
        # `verify_failed_maker_worktree()` already reports as `maker_partial_worktree` for a
        # failed Maker -- only this successful (exit 0) Maker never reached that failure path.
        # Without this conversion, `finalize_ephemeral_git()` propagates a plain
        # `EphemeralGitInfrastructureError` that `loop_docker_action._dispatch()` treats as an
        # opaque Docker infrastructure failure, and by the time its `abort()` fallback would run
        # `verify_failed_maker_worktree()` itself, the runtime is already latched `_finished` and
        # silently no-ops (see `DockerActionRuntime.finish()`/`abort()`).
        converted = _as_partial_worktree_stop(exc)
        if converted is exc:
            raise
        raise converted from exc

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


def _snapshot_local_overrides_or_raise(
    worktree_path: Path,
    *,
    safety_stop: bool = False,
) -> tuple[LocalOverrideSnapshot, ...]:
    """Snapshot local overrides, normalizing a read failure to a typed Git error.

    Codex review, PR #262, P2 (round 8): `safety_stop` (default `False`, matching every
    pre-existing call site) must mirror the caller's own classification intent. Before this,
    a `LocalOverrideSnapshotError` here -- e.g. `loop_local_override_guard`'s own fail-closed
    detection of a symlinked project-local override directory the Maker swapped in -- always
    became a plain `EphemeralGitInfrastructureError`, even when called from
    `_verify_local_override_snapshot(session, safety_stop=True)` below. That silently
    downgraded a Maker-tampering safety stop into an ordinary "infrastructure hiccup" that
    `loop_docker_action._dispatch()` retries/reports as opaque Docker infra failure instead of
    the durable safe-stop the caller's `safety_stop=True` explicitly asked for.
    """
    try:
        return snapshot_local_overrides(worktree_path)
    except LocalOverrideSnapshotError as exc:
        if safety_stop:
            raise EphemeralGitSafetyStop(
                "maker_partial_worktree",
                f"could not verify project-local configuration overrides: {exc}",
            ) from exc
        raise EphemeralGitInfrastructureError(str(exc)) from exc


def _verify_local_override_snapshot(
    session: EphemeralGitSession,
    *,
    safety_stop: bool,
) -> None:
    actual = _snapshot_local_overrides_or_raise(session.worktree_path, safety_stop=safety_stop)
    if actual == session.local_override_snapshot:
        return
    changed_paths = changed_local_override_paths(session.local_override_snapshot, actual)
    details = {"changed_local_overrides": changed_paths}
    if safety_stop:
        raise EphemeralGitSafetyStop(
            "maker_partial_worktree",
            "Maker changed project-local configuration overrides",
            details=details,
        )
    raise EphemeralGitInfrastructureError(
        "project-local configuration overrides changed while preparing ephemeral git",
        details=details,
    )


def _widen_tree_permissions(root: Path) -> None:
    """Grant the owner rwx on `root` and every directory beneath it, recursively.

    Codex review, PR #262, P1 (round 12): `loop_docker_settings.create_settings_bundle()`
    deliberately chmods its `trusted-settings` directory (and the files inside it) down to
    0o555/0o444 once populated, so a Maker/Checker container mounted read-only there cannot
    tamper with its own guard/settings. `discard_after_lease_loss()` (round 11) intentionally
    skips the matching `cleanup_settings_bundle()` chmod(0o700) restore -- see that method's
    own docstring -- so the next retry of the same action_id reaches `_remove_runtime()` below
    via `prepare_ephemeral_git()`'s unconditional wipe-and-recreate of `runtime_dir`, which
    still contains that 0o555 `trusted-settings` subdirectory. Removing entries *inside* a
    0o555 directory needs write permission on that directory itself, not on the entries -- a
    non-root driver process never has that once chmod(0o555) ran (unlike a root driver, which
    bypasses permission checks entirely and never observes this failure). Only directory modes
    are widened here, never file modes: unlinking a file only needs write+execute on its
    containing directory, not any permission bit on the file's own mode, so a 0o600/0o444
    secret or settings file keeps its own restrictive mode completely untouched right up until
    `shutil.rmtree()` removes it. `os.walk(followlinks=False)` only stops `os.walk()` itself from
    *descending into* a symlinked directory -- it still yields the symlink's own name in
    `dirnames` for the directory that contains it. `os.chmod()` follows symlinks by default
    (`follow_symlinks=True`), so without an explicit `os.path.islink()` guard here, a
    world-writable `root` containing e.g. `evil -> /etc` would have this walk `chmod(0o700)`
    the real `/etc` the symlink points at, not the symlink itself -- especially dangerous for a
    root-privileged retry driver. Every entry in `dirnames` is therefore checked with
    `os.path.islink()` and skipped if it is a symlink, before ever calling `os.chmod()` on it (a
    per-platform `follow_symlinks=False` chmod is not used instead because it is unsupported on
    some platforms, e.g. raising `NotImplementedError` on Linux). Every `os.chmod()` failure is
    swallowed on purpose: this is a best-effort pre-widening step for the immediately-following
    `shutil.rmtree()`, which still raises (and gets wrapped into
    `EphemeralGitInfrastructureError`) on any real removal failure.
    """
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    for current, dirnames, _filenames in os.walk(root, followlinks=False):
        for name in dirnames:
            entry_path = os.path.join(current, name)
            if os.path.islink(entry_path):
                continue
            try:
                os.chmod(entry_path, 0o700)
            except OSError:
                pass


def _remove_runtime(runtime_dir: Path) -> None:
    try:
        if runtime_dir.is_symlink() or runtime_dir.is_file():
            runtime_dir.unlink(missing_ok=True)
        elif runtime_dir.exists():
            _widen_tree_permissions(runtime_dir)
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
