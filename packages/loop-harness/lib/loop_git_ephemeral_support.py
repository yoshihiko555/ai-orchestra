#!/usr/bin/env python3
"""Internal Git plumbing helpers for ``loop_git_ephemeral``.

This module holds the trusted-runtime validation and low-level git execution
helpers that back ``prepare_ephemeral_git`` / ``finalize_ephemeral_git`` in
``loop_git_ephemeral.py``. It is a pure implementation-detail split (Codex
review, PR #256, Critical -- ``loop_git_ephemeral.py`` exceeded the 800-line
file limit in ``.claude/rules/coding-principles.md``); the public API and
security boundary are unchanged. Only ``loop_git_ephemeral.py`` imports from
this module -- it is not a standalone public entry point.

``EphemeralGitSafetyStop`` / ``EphemeralGitInfrastructureError`` are defined
here (nearly every helper below raises one of them) and re-exported by
``loop_git_ephemeral.py`` so existing ``loop_git_ephemeral.EphemeralGit*``
references keep working unchanged.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import loop_driver_support as lds  # noqa: E402

if TYPE_CHECKING:
    from loop_git_ephemeral import EphemeralGitSession

_GIT_TIMEOUT_SECONDS = 30.0
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# Git repository-location variables that must never leak from the *ambient* environment of the
# host driver process into a host-invoked git subprocess (Codex review, PR #256, Critical). If
# the calling Python process itself happens to run with e.g. GIT_INDEX_FILE set (for example
# when loop-harness runs inside another git hook/wrapper), naively inheriting os.environ would
# silently redirect `read-tree`/`status` writes away from the ephemeral or trusted-status index
# this module explicitly manages, letting a stale or attacker-influenced ambient index seed the
# Maker-writable index -- or the trusted-tree comparison -- instead of the one derived from
# `-C <path>` / `--git-dir <path>`. Every git process this module spawns on the host must have
# these vars scrubbed from any inherited environment; call sites that need one set explicitly do
# so via an override applied after scrubbing.
#
# Codex review, PR #262, P2 (round 12): `GIT_TEMPLATE_DIR` is scrubbed for the same reason but
# specifically protects the `git init --bare` call in `loop_git_ephemeral.py` from copying
# owner-only-permission hook files (or anything else) out of an ambient/attacker-controlled
# template directory into the freshly created `ephemeral_dir`.
_GIT_LOCATION_ENV_VARS = (
    "GIT_INDEX_FILE",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_TEMPLATE_DIR",
)

GitRunner = Callable[..., subprocess.CompletedProcess[str]]


def _stripped_host_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Base env for host-invoked git processes, purged of ambient repository-location vars.

    See ``_GIT_LOCATION_ENV_VARS`` for the rationale. ``overrides`` is applied last and always
    wins, so callers that intentionally need e.g. ``GIT_DIR``/``GIT_WORK_TREE``/``GIT_INDEX_FILE``
    pointed at a specific, trusted path pass it here rather than relying on ambient inheritance.
    """
    env = {key: value for key, value in os.environ.items() if key not in _GIT_LOCATION_ENV_VARS}
    if overrides:
        env.update(overrides)
    return env


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
    return _stripped_host_env(
        {
            "GIT_DIR": str(session.ephemeral_dir),
            "GIT_WORK_TREE": str(session.worktree_path),
        }
    )


def _verify_worktree_matches_trusted_tree(
    *,
    git_dir: Path,
    worktree_path: Path,
    target_sha: str,
    temp_index_dir: Path,
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
    freshly-read index never has them set), and diffs the worktree against that.

    Fix-10 (PR #256 review, High): the same helper is reused by
    ``prepare_ephemeral_git`` (design doc §4.3.1 step 11) with ``git_dir=<common_dir>`` and
    ``target_sha=<baseline_sha>``, run *before* the Maker-writable ephemeral GIT_DIR is created.
    ``worktree_manager.create_worktree()`` may hand back a worktree reused from a previous,
    interrupted action; without this check, leftover uncommitted changes from that earlier action
    would silently ride along into the new ephemeral index seed and can later pass finalize's own
    trusted-tree check (a candidate commit built partly from stale content still resolves to a
    tree that is internally consistent). Running the identical trusted-index comparison at both
    prepare and finalize closes the same class of gap at the worktree's two points of entry into
    the write-back path.

    Fix-15 (docs/design/loop-harness-isolation.md §10, 2026-07-18, Critical -- discovered while
    writing the ``prepare``-side regression test for Fix-10): this helper originally ran
    ``git status --porcelain`` against the freshly seeded temporary index. Porcelain status has two
    columns -- staged (index vs ``HEAD``) and unstaged (worktree vs index) -- and prepare's call
    passes ``git_dir=<common_dir>``, so ``HEAD`` there resolves to whatever the *primary* worktree
    (typically ``main``) currently points at, not to ``target_sha``/``baseline_sha``. Any time the
    primary worktree's branch has advanced independently of the Maker linked worktree's branch (a
    normal, constant occurrence in real usage -- e.g. another PR merging into ``main`` while an
    action is in flight), the staged column reports every path that differs between the seeded
    index and that unrelated ``HEAD`` as dirty, even though the Maker worktree itself has zero
    actual drift, making prepare fail-closed unconditionally. The check's intent has always been
    "does the worktree's file content match ``target_sha``'s tree", which is entirely independent
    of wherever the primary worktree's ``HEAD`` happens to point. This is fixed by comparing the
    worktree only against the seeded index -- never against ``HEAD`` -- via three HEAD-independent
    commands instead of porcelain status: ``update-index --refresh`` followed by
    ``diff-files --name-status`` (working tree vs the index only) for tracked-file drift, and
    ``ls-files --others --exclude-standard`` (worktree entries absent from the index) for untracked
    residue. None of the three consults ``HEAD`` at all.

    Two single-command alternatives were tried first and rejected, for reasons load-bearing enough
    to record here:

    - Plain ``diff-files --name-status`` alone (no prior refresh): it reports every tracked path as
      modified whenever the index's cached stat info doesn't match the working tree file's raw stat
      (always true right after ``read-tree``, which seeds zeroed stat fields), because in its "raw"
      output mode it never falls back to an actual content comparison to disprove a stat mismatch --
      a second, independent source of false-positive "dirty" reports on an untouched worktree.
    - No-revision ``git diff --name-status`` (the porcelain form of the same comparison): unlike
      raw ``diff-files``, it *does* fall back to a real comparison on stat mismatch, but to build
      that comparison it must read the *index-recorded* blob's actual object content, which fails
      outright (non-zero exit, not a "dirty" verdict) if that object is not locally resolvable --
      exactly the state Fix-7's alternates restoration (which always runs immediately before this
      check at finalize) can legitimately leave a *maliciously* fabricated commit's tree in. That
      case must fall through to finalize's later fetch step so it fails there as
      ``git_ref_import_failed`` (the existing, tested confused-deputy safe-stop), not abort early
      here with an unrelated infrastructure error.

    ``update-index --refresh`` sidesteps both problems: for each stat-mismatched entry it computes
    a fresh hash of the *working tree* file only and string-compares it against the OID already
    recorded in the index entry -- it never needs to read the recorded blob's actual bytes, so an
    unresolvable-but-content-matching foreign OID (as in the scenario above) still refreshes clean.
    Paths it cannot refresh clean keep their stale/mismatched cache entry, so the follow-up
    ``diff-files --name-status`` (also never touching blob content in raw/name-status mode) reports
    exactly the genuinely-changed and genuinely-deleted paths, and nothing else. ``refresh``'s exit
    code is intentionally not checked -- a non-zero "needs update" result is the expected, normal
    outcome whenever real drift exists; ``diff-files`` afterwards is the sole source of truth for
    the tracked-file dirty verdict.
    """
    temporary_index = temp_index_dir / f".status-index.loop-harness-{secrets.token_hex(8)}"
    env = _stripped_host_env(
        {
            "GIT_DIR": str(git_dir),
            "GIT_WORK_TREE": str(worktree_path),
            "GIT_INDEX_FILE": str(temporary_index),
        }
    )
    try:
        _run_git(
            ["read-tree", target_sha],
            runner=runner,
            env=env,
            operation="seed the trusted status index from the target tree",
        )
        # Reconcile the index's zeroed-out post-`read-tree` stat cache against the real working
        # tree without ever reading the *recorded* blob content (Fix-15) -- see docstring above.
        # A non-zero/`needs update` result here is expected whenever real drift exists; it is not
        # an error, so this intentionally uses the unchecked runner rather than `_run_git`.
        _run_git_unchecked(
            ["update-index", "--refresh", "-q"],
            runner=runner,
            env=env,
            operation="refresh the trusted status index stat cache",
        )
        # HEAD-independent: the raw (non-porcelain) `diff-files` compares the working tree against
        # the index only and, in `--name-status` mode, never reads blob content -- it only string-
        # compares recorded vs. (already refreshed above) cached-file OIDs (Fix-15).
        tracked_diff_result = _run_git(
            ["diff-files", "--name-status", "--no-renames"],
            runner=runner,
            env=env,
            operation="check tracked worktree files against the trusted tree",
        )
        untracked_result = _run_git(
            ["ls-files", "--others", "--exclude-standard"],
            runner=runner,
            env=env,
            operation="check the worktree for untracked files against the trusted tree",
        )
    finally:
        try:
            temporary_index.unlink(missing_ok=True)
        except OSError:
            pass
    tracked_dirty_lines = [line for line in tracked_diff_result.stdout.splitlines() if line.strip()]
    untracked_dirty_lines = [
        status_line
        for path in untracked_result.stdout.splitlines()
        if path.strip()
        for status_line in (f"?? {path}",)
        if not _is_untracked_local_override(status_line)
    ]
    dirty_lines = tracked_dirty_lines + untracked_dirty_lines
    if dirty_lines:
        raise EphemeralGitInfrastructureError(
            "worktree status is dirty relative to the trusted target tree",
            details={"status": "\n".join(dirty_lines), "target_sha": target_sha},
        )


_LOCAL_OVERRIDE_ROOT = ".claude/config/"


def _is_untracked_local_override(status_line: str) -> bool:
    """True for an untracked ``.claude/config/**/*.local.{yaml,json}`` porcelain line.

    CodeRabbit (PR #256 review, Major): ``.claude/rules/config-loading.md`` treats
    ``*.local.yaml``/``*.local.json`` as intentional, git-ignored project overrides that must
    never be clobbered or blocked on. ``git status --porcelain`` still reports them as untracked
    (``??``) when a worktree is reused without those overrides being gitignored in that
    repository, which would otherwise make ``_verify_worktree_matches_trusted_tree`` reject an
    otherwise-clean worktree. Only untracked local-override files are excluded here; any tracked
    change (staged or unstaged) still fails the dirty check unchanged, preserving the leftover-
    Maker-residue detection this function exists for.
    """
    if not status_line.startswith("?? "):
        return False
    path = status_line[3:]
    if path.startswith('"') and path.endswith('"') and len(path) >= 2:
        path = path[1:-1]
    if not path.startswith(_LOCAL_OVERRIDE_ROOT):
        return False
    return path.endswith(".local.yaml") or path.endswith(".local.json")


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


def _harden_ephemeral_git_metadata(session: EphemeralGitSession) -> None:
    """Restore trusted metadata and reject object-store symlinks before host use or mounting.

    Maker finalize and Checker read-only mount preparation share this boundary. Keeping the
    operations together prevents the Checker path from losing either the trusted-config restore,
    the forced single-line alternates restore, or the recursive objects symlink rejection when the
    Phase 2 defenses evolve.
    """
    _restore_ephemeral_git_config(session)
    _restore_ephemeral_git_alternates(session)


def _validate_common_objects_mount_source(session: EphemeralGitSession) -> Path:
    """Return the shared objects directory only when it is a real directory, never a symlink.

    A read-only bind mount still follows its source symlink. Without this check, a repository whose
    ``common_dir/objects`` points back to ``common_dir`` could expose shared refs, config, hooks, and
    reflogs to an untrusted Checker despite the mount spec naming only ``objects``.
    """
    common_objects = session.common_dir / "objects"
    try:
        metadata = common_objects.stat(follow_symlinks=False)
    except OSError as exc:
        raise EphemeralGitInfrastructureError(
            "shared git objects mount source is not accessible",
            details={"common_objects": str(common_objects)},
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise EphemeralGitInfrastructureError(
            "shared git objects mount source is not a trusted directory",
            details={"common_objects": str(common_objects)},
        )
    return common_objects


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


def _verify_checker_baseline_matches_branch_tip(
    session: EphemeralGitSession,
    *,
    runner: GitRunner,
) -> None:
    """Reject a session whose pinned baseline is not the branch's current tip.

    Design doc §4.3.4 step 1 requires the Checker mount spec to be built from a session whose
    baseline is "the branch tip at Checker execution time" -- i.e. a session prepared fresh for
    this Checker run, not a Maker session still in flight (whose pinned baseline is necessarily
    the *older* tip captured before that Maker action started) or a session left over from an
    earlier, unrelated action. ``build_checker_git_mount_spec`` only receives an
    ``EphemeralGitSession``, so nothing at the type level distinguishes a fresh Checker session
    from a stale or wrong one. This check turns the "new, tip-baselined session" requirement into
    a runtime invariant instead of a documentation-only convention: it re-resolves
    ``session.branch_ref`` against the shared, trusted ``common_dir`` (the same host-only,
    ambient-env-scrubbed lookup every other trust-boundary check in this module uses) and fails
    closed the moment the pinned ``baseline_sha`` no longer matches the branch's live tip.

    Codex review (PR #258, High): the ``common_dir`` tip comparison above only catches a Maker
    session that already *finalized* -- i.e. ``finalize_ephemeral_git`` already ran its CAS
    write-back into the shared branch. A Maker session that is still in flight (or crashed) after
    committing into its own Maker-writable ``session.ephemeral_dir``, but *before* that CAS
    write-back happened, leaves the shared branch tip untouched at ``baseline_sha`` -- the check
    above passes even though ``session.ephemeral_dir`` now holds a candidate commit (or dirty
    worktree edits) that were never written back to the shared branch and must never be handed to
    a Checker as "the fresh baseline". The two calls below close that gap by validating the two
    concrete places such in-flight Maker output would show up, in addition to the shared branch
    tip: the session's own ephemeral ref, and the shared worktree's file content.
    """
    tip_result = _run_git(
        ["--git-dir", session.common_dir, "rev-parse", "--verify", session.branch_ref],
        runner=runner,
        operation="resolve current branch tip before building the Checker mount spec",
    )
    current_tip = tip_result.stdout.strip()
    if current_tip != session.baseline_sha:
        raise EphemeralGitInfrastructureError(
            "checker session baseline does not match the branch's current tip",
            details={
                "branch_ref": session.branch_ref,
                "baseline_sha": session.baseline_sha,
                "current_tip": current_tip,
            },
        )
    _verify_checker_ephemeral_ref_not_advanced(session, runner=runner)
    _verify_worktree_matches_trusted_tree(
        git_dir=session.common_dir,
        worktree_path=session.worktree_path,
        target_sha=session.baseline_sha,
        temp_index_dir=session.runtime_dir,
        runner=runner,
    )


def _verify_checker_ephemeral_ref_not_advanced(
    session: EphemeralGitSession,
    *,
    runner: GitRunner,
) -> None:
    """Reject a session whose own ephemeral GIT_DIR branch ref has moved past baseline.

    Codex review (PR #258, High): ``prepare_ephemeral_git`` always seeds
    ``session.ephemeral_dir``'s ``session.branch_ref`` to exactly ``session.baseline_sha`` (see
    ``_prepare_ephemeral_git``'s "seed ephemeral branch" step). A Maker container committing into
    that Maker-writable ephemeral repository necessarily moves this ref -- that is the only way a
    Maker action can ever record a candidate change -- regardless of whether
    ``finalize_ephemeral_git`` has since run to fast-forward the shared ``common_dir`` branch (the
    ``_verify_checker_baseline_matches_branch_tip`` check above only observes *that* write-back,
    not this one). Comparing the ephemeral ref directly is therefore an independent, narrower
    signal of "has this session's Maker container committed" that does not depend on finalize
    having run at all. ``rev-parse --verify --quiet`` is used rather than ``show-ref --verify``:
    both read the ref value straight out of ``session.ephemeral_dir``, but ``show-ref --verify``
    additionally requires the referenced commit object itself to be resolvable (it fails with
    ``bad ref`` otherwise), which routes through ``objects/info/alternates`` -- a file this check
    must stay valid regardless of, since ``build_checker_git_mount_spec`` calls this *before*
    ``_harden_ephemeral_git_metadata`` restores it. ``rev-parse --verify --quiet`` resolves the
    same ref to the same SHA without that object-resolvability requirement.
    """
    ref_result = _run_git(
        [
            "--git-dir",
            session.ephemeral_dir,
            "rev-parse",
            "--verify",
            "--quiet",
            session.branch_ref,
        ],
        runner=runner,
        operation="resolve the ephemeral branch ref before building the Checker mount spec",
    )
    ephemeral_tip = ref_result.stdout.strip()
    if ephemeral_tip != session.baseline_sha:
        raise EphemeralGitInfrastructureError(
            "checker session ephemeral branch ref has advanced past its pinned baseline",
            details={
                "branch_ref": session.branch_ref,
                "baseline_sha": session.baseline_sha,
                "ephemeral_tip": ephemeral_tip or None,
            },
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
    # Codex review, PR #256, Critical: when the caller does not pass an explicit env (the common
    # case -- e.g. `-C <path>` / `--git-dir <path>` commands), fall back to a *scrubbed* copy of
    # the ambient environment rather than inheriting it verbatim. See `_GIT_LOCATION_ENV_VARS`.
    # Callers that build an explicit env themselves (`_ephemeral_env`,
    # `_verify_worktree_matches_trusted_tree`) already route through `_stripped_host_env`, so
    # their intentional GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE overrides are preserved unchanged.
    git_env = _stripped_host_env() if env is None else dict(env)
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
