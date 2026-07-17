#!/usr/bin/env python3
"""Prepare and safely write back Maker-owned ephemeral Git metadata.

This module is deliberately limited to Git plumbing and runtime-directory cleanup. It does
not read or write loop state, journals, or notifications; callers translate the typed results
and failures into the loop protocol at the orchestration boundary.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import loop_driver_support as lds  # noqa: E402

_GIT_TIMEOUT_SECONDS = 30.0
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

GitRunner = Callable[..., subprocess.CompletedProcess[str]]


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


class EphemeralGitSafetyStop(RuntimeError):
    """A user-visible write-back safety stop with a public stop_reason."""

    def __init__(
        self,
        stop_reason: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.stop_reason = stop_reason
        self.details = dict(details or {})


class EphemeralGitInfrastructureError(RuntimeError):
    """An execution or local-runtime failure that is not a safety classification."""

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = dict(details or {})


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
    _verify_worktree_matches_trusted_tree(session, new_sha, runner=runner)

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


def _normalize_branch_ref(branch: str, *, runner: GitRunner) -> str:
    branch_ref = branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"
    result = _run_git_unchecked(
        ["check-ref-format", branch_ref],
        runner=runner,
        operation="validate branch ref",
    )
    if result.returncode != 0:
        raise ValueError(f"Invalid branch ref: {branch}")
    return branch_ref


def _validate_safe_id(field: str, value: str) -> None:
    if not _SAFE_ID_RE.fullmatch(value) or ".." in value:
        raise ValueError(f"Unsafe {field}: {value}")


def _validate_runtime_location(project: Path, worktree: Path, runtime_dir: Path) -> None:
    try:
        runtime_dir.relative_to(worktree)
    except ValueError:
        pass
    else:
        raise EphemeralGitInfrastructureError(
            "ephemeral git runtime must remain outside the Maker worktree",
            details={"runtime_dir": str(runtime_dir), "worktree": str(worktree)},
        )

    try:
        relative = runtime_dir.parent.relative_to(project)
    except ValueError as exc:  # pragma: no cover - paths are derived above
        raise EphemeralGitInfrastructureError(
            "ephemeral git runtime is outside the root worktree",
            details={"runtime_dir": str(runtime_dir), "project": str(project)},
        ) from exc
    current = project
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise EphemeralGitInfrastructureError(
                "ephemeral git runtime parent must not be a symlink",
                details={"path": str(current)},
            )
        if current.exists() and not current.is_dir():
            raise EphemeralGitInfrastructureError(
                "ephemeral git runtime parent is not a directory",
                details={"path": str(current)},
            )


def _verify_checked_out_branch(
    worktree: Path,
    branch_ref: str,
    *,
    runner: GitRunner,
    operation: str,
) -> None:
    result = _run_git_unchecked(
        ["-C", worktree, "symbolic-ref", "--quiet", "HEAD"],
        runner=runner,
        operation=operation,
    )
    actual_ref = result.stdout.strip()
    if result.returncode != 0 or actual_ref != branch_ref:
        raise EphemeralGitInfrastructureError(
            "worktree checked-out branch does not match the requested branch",
            details={
                "expected_branch_ref": branch_ref,
                "actual_branch_ref": actual_ref or None,
                "returncode": result.returncode,
            },
        )


def _ephemeral_env(session: EphemeralGitSession) -> dict[str, str]:
    return {
        **os.environ,
        "GIT_DIR": str(session.ephemeral_dir),
        "GIT_WORK_TREE": str(session.worktree_path),
    }


def _verify_worktree_matches_trusted_tree(
    session: EphemeralGitSession,
    target_sha: str,
    *,
    runner: GitRunner,
) -> None:
    """Reject worktree drift from ``target_sha`` using a fresh, host-owned index.

    Fix-9 (PR #256 review, High): the ephemeral index at `<ephemeral_dir>/index` lives inside a
    Maker-writable, container-mounted directory, so `git status --porcelain` against *that* index
    is something Maker fully controls -- most simply by marking a changed path
    `--skip-worktree`/`--assume-unchanged` inside the container, which makes `status` silently
    stop reporting drift on that path even though its worktree content no longer matches what
    Maker committed (or, for the no-commit case, no longer matches baseline). This function
    instead points `GIT_INDEX_FILE` at a brand-new file only host code ever writes to, seeds it
    purely from `target_sha`'s tree via `read-tree` (which never carries forward skip-worktree /
    assume-unchanged bits -- those live in index extensions, not in the tree object, so a
    freshly-read index never has them set), and diffs the worktree against that. Running this
    once, unconditionally, before the no-commit/commit branch is decided closes the same class of
    gap for *both* paths: `target_sha == baseline_sha` on the no-commit path catches Maker changes
    left uncommitted in the worktree, and `target_sha == new_sha` on the commit path replaces the
    dirty-after-commit check this function supersedes.
    """
    temporary_index = session.runtime_dir / f".status-index.loop-harness-{secrets.token_hex(8)}"
    env = {
        **os.environ,
        "GIT_DIR": str(session.ephemeral_dir),
        "GIT_WORK_TREE": str(session.worktree_path),
        "GIT_INDEX_FILE": str(temporary_index),
    }
    try:
        _run_git(
            ["read-tree", target_sha],
            runner=runner,
            env=env,
            operation="seed the trusted status index from the target tree",
        )
        status_result = _run_git(
            ["status", "--porcelain"],
            runner=runner,
            env=env,
            operation="check worktree status against the trusted tree",
        )
    finally:
        try:
            temporary_index.unlink(missing_ok=True)
        except OSError:
            pass
    if status_result.stdout:
        raise EphemeralGitInfrastructureError(
            "worktree status is dirty relative to the trusted target tree",
            details={"status": status_result.stdout, "target_sha": target_sha},
        )


def _restore_ephemeral_git_config(session: EphemeralGitSession) -> None:
    """Atomically replace Maker-owned config before any host Git process can read it."""
    try:
        if session.pinned_git_config.is_symlink() or not session.pinned_git_config.is_file():
            raise EphemeralGitInfrastructureError(
                "trusted ephemeral git config snapshot is not a regular file",
                details={"pinned_git_config": str(session.pinned_git_config)},
            )
        trusted_config = session.pinned_git_config.read_bytes()
        temporary = session.ephemeral_dir / f".config.loop-harness-{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(trusted_config)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, session.ephemeral_dir / "config")
    except EphemeralGitInfrastructureError:
        raise
    except OSError as exc:
        if "temporary" in locals():
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise EphemeralGitInfrastructureError(
            "failed to restore the trusted ephemeral git config",
            details={"ephemeral_dir": str(session.ephemeral_dir)},
        ) from exc


def _restore_ephemeral_git_alternates(session: EphemeralGitSession) -> None:
    """Atomically force objects/info/alternates back to the one trusted common-objects line.

    Confused-deputy fix (Issue #211 Phase 2 review, Critical): `<ephemeral_dir>` is a Maker-owned
    rw directory, so `objects/info/alternates` is just as attacker-controlled as `config` (which
    `_restore_ephemeral_git_config` already neutralizes). Unlike `config`, the trusted content
    here does not need a pinned snapshot -- it is always exactly one line derived from
    `session.common_dir`, which is itself part of the trusted session. If Maker rewrites
    `alternates` to point at an arbitrary object store reachable by the *host* driver process
    (not necessarily reachable from inside the container) and fabricates a baseline-descendant
    commit whose tree references objects that only resolve through that rewritten alternates
    file, the fast-forward ancestry check alone would not catch it (ancestry only walks the
    commit-parent graph). The subsequent host-side `fetch` from `<ephemeral_dir>` would then
    permanently copy those foreign objects into the shared `common_dir/objects`. Restoring this
    file at the very start of finalize -- the same point `_restore_ephemeral_git_config` runs at,
    before any host git process (including the `rev-parse` a few lines below) touches
    `<ephemeral_dir>` -- closes that path entirely: by the time anything reads through
    `alternates`, it can only ever resolve into the already-shared, already-trusted object store.

    `objects/info/http-alternates` (a legacy alternate-object-source mechanism `git fetch`/
    `upload-pack` also honors) is removed outright for the same reason; loop-harness never writes
    one, so any presence of this file is itself evidence of tampering.

    Loose objects or pack files Maker writes directly under `<ephemeral_dir>/objects/` (outside of
    `info/`) through legitimate `git add`/`git commit` are not restricted here -- their *content*
    is indistinguishable from any other object the container legitimately creates, and bounding
    their size/count is a DoS quota concern tracked separately (Issue #255). What *is* rejected
    here is any `objects/` entry, at any depth, that is a symlink rather than a real file or
    directory (Fix-8, PR #256 review, Critical): a Maker-owned symlink under e.g. `objects/ab/`
    (a loose-object fanout directory) or `objects/pack` pointed at an external repository's object
    store lets host Git resolve foreign objects it finds there when it walks `<ephemeral_dir>`
    directly (`rev-parse`, `read-tree`, the later `fetch`), even though `objects/info/alternates`
    itself is restored to the one trusted line above -- this is a second, independent path into the
    same confused-deputy class of bug that Fix-7 closed for `alternates`.
    """
    objects_dir = session.ephemeral_dir / "objects"
    info_dir = objects_dir / "info"
    for path in (objects_dir, info_dir):
        if path.is_symlink():
            raise EphemeralGitInfrastructureError(
                "ephemeral git objects directory has been replaced with a symlink",
                details={"path": str(path)},
            )
        if path.exists() and not path.is_dir():
            raise EphemeralGitInfrastructureError(
                "ephemeral git objects directory is not a directory",
                details={"path": str(path)},
            )
    _reject_symlinks_under_objects(objects_dir)
    try:
        info_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EphemeralGitInfrastructureError(
            "failed to recreate the ephemeral git objects/info directory",
            details={"info_dir": str(info_dir)},
        ) from exc

    http_alternates = info_dir / "http-alternates"
    if http_alternates.is_symlink() or http_alternates.exists():
        try:
            http_alternates.unlink()
        except OSError as exc:
            raise EphemeralGitInfrastructureError(
                "failed to remove the untrusted ephemeral git http-alternates file",
                details={"http_alternates": str(http_alternates)},
            ) from exc

    trusted_alternates = f"{session.common_dir / 'objects'}\n".encode()
    alternates = info_dir / "alternates"
    temporary = info_dir / f".alternates.loop-harness-{secrets.token_hex(8)}"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(trusted_alternates)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, alternates)
    except OSError as exc:
        if temporary.exists():
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise EphemeralGitInfrastructureError(
            "failed to restore the trusted ephemeral git alternates file",
            details={"ephemeral_dir": str(session.ephemeral_dir)},
        ) from exc


def _reject_symlinks_under_objects(objects_dir: Path) -> None:
    """Recursively reject any symlink anywhere under ``objects_dir`` without following it.

    Fix-8 (PR #256 review, Critical): a naive `Path.rglob`/`os.walk(followlinks=True)` scan would
    itself descend into an attacker-planted symlinked directory before this function gets a chance
    to reject it. Walking with an explicit stack and `os.scandir` -- which reports
    `DirEntry.is_symlink()` from the raw `lstat` the kernel already did for us, without a second
    syscall -- and only ever pushing entries that are directories with `follow_symlinks=False`
    guarantees the traversal never crosses a symlink boundary, so a symlinked fanout directory
    (e.g. `objects/ab`) or `objects/pack` is caught at the moment it is first observed.
    """
    if not objects_dir.is_dir() or objects_dir.is_symlink():
        return
    pending = [objects_dir]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise EphemeralGitInfrastructureError(
                "failed to scan the ephemeral git objects directory for symlinks",
                details={"path": str(current)},
            ) from exc
        for entry in entries:
            if entry.is_symlink():
                raise EphemeralGitInfrastructureError(
                    "ephemeral git objects directory contains a symlink",
                    details={"path": entry.path},
                )
            if entry.is_dir(follow_symlinks=False):
                pending.append(Path(entry.path))


def _verify_git_pointer(session: EphemeralGitSession) -> None:
    git_pointer = session.worktree_path / ".git"
    if git_pointer.is_symlink():
        raise EphemeralGitInfrastructureError(
            "worktree .git pointer differs from its trusted pinned snapshot",
            details={"git_pointer": str(git_pointer)},
        )
    try:
        current = git_pointer.read_bytes()
        pinned = session.pinned_git_pointer.read_bytes()
    except OSError as exc:
        raise EphemeralGitInfrastructureError(
            "could not verify the worktree .git pointer",
            details={"git_pointer": str(git_pointer)},
        ) from exc
    if not git_pointer.is_file() or current != pinned:
        raise EphemeralGitInfrastructureError(
            "worktree .git pointer differs from its trusted pinned snapshot",
            details={"git_pointer": str(git_pointer)},
        )


def _delete_import_ref(session: EphemeralGitSession, *, runner: GitRunner) -> None:
    result = _run_git_unchecked(
        ["--git-dir", session.common_dir, "update-ref", "-d", session.import_ref],
        runner=runner,
        operation="delete temporary import ref",
    )
    if result.returncode != 0:
        raise EphemeralGitInfrastructureError(
            "failed to delete the temporary import ref",
            details=_command_failure_details(result),
        )


def _delete_import_ref_best_effort(session: EphemeralGitSession, *, runner: GitRunner) -> None:
    try:
        _delete_import_ref(session, runner=runner)
    except EphemeralGitInfrastructureError:
        pass


def _delete_import_ref_error(
    session: EphemeralGitSession,
    *,
    runner: GitRunner,
) -> EphemeralGitInfrastructureError | None:
    try:
        _delete_import_ref(session, runner=runner)
    except EphemeralGitInfrastructureError as exc:
        return exc
    return None


def _attach_cleanup_error(primary: BaseException, cleanup: Exception) -> None:
    primary.add_note(f"additional cleanup failure: {cleanup}")
    if isinstance(primary, (EphemeralGitSafetyStop, EphemeralGitInfrastructureError)):
        primary.details["cleanup_error"] = {
            "message": str(cleanup),
            "details": getattr(cleanup, "details", {}),
        }


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


def _run_git(
    args: Sequence[object],
    *,
    runner: GitRunner,
    operation: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = _run_git_unchecked(args, runner=runner, operation=operation, env=env)
    if result.returncode != 0:
        raise EphemeralGitInfrastructureError(
            f"git failed to {operation}",
            details=_command_failure_details(result),
        )
    return result


def _run_git_unchecked(
    args: Sequence[object],
    *,
    runner: GitRunner,
    operation: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["git", *lds.hardened_git_config_args(), *args]
    git_env = dict(os.environ) if env is None else dict(env)
    git_env["GIT_CONFIG_GLOBAL"] = os.devnull
    git_env["GIT_CONFIG_NOSYSTEM"] = "1"
    try:
        return runner(
            command,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            env=git_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EphemeralGitInfrastructureError(
            f"could not execute git to {operation}",
            details={"operation": operation},
        ) from exc


def _command_failure_details(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "returncode": result.returncode,
        "stderr": (result.stderr or "").strip(),
    }
