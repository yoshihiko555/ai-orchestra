#!/usr/bin/env python3
"""Content-free snapshots for user-owned project-local configuration overrides."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

_LOCAL_OVERRIDE_SUFFIXES = (".local.yaml", ".local.json")
# PR #262 push-front adversarial review, P2 (round 10): chunk size used to stream-hash a
# symlinked override's target so an ordinary but huge target file cannot balloon memory use
# during a snapshot. Unlike the previous fixed 10 MiB *read cap* this bounds only the
# per-iteration read; the whole file is still folded into the digest (see
# `_resolved_symlink_target_digest()` below), so there is no size past which tampering goes
# undetected. Tests may monkeypatch this to a small value to exercise multi-chunk hashing
# cheaply, without needing to materialize a multi-megabyte fixture.
_SYMLINK_TARGET_HASH_CHUNK_BYTES = 1024 * 1024


class LocalOverrideSnapshotError(RuntimeError):
    """A project-local override snapshot could not be read safely."""


@dataclass(frozen=True)
class LocalOverrideSnapshot:
    """Path, type, and digest identity without retaining credential-like contents."""

    path: str
    kind: str
    mode: int
    uid: int
    gid: int
    device: int
    inode: int
    link_count: int
    digest: str


def snapshot_local_overrides(worktree_path: Path) -> tuple[LocalOverrideSnapshot, ...]:
    """Snapshot every ``.claude/config/**/*.local.{yaml,json}`` entry."""
    root = worktree_path / ".claude" / "config"
    if root.is_symlink():
        # Codex review, PR #262, High (round 5): a symlinked config root is not a supported
        # project layout (no sync/scaffolding tool in this codebase creates one), but if a
        # worktree ever has one, `load_config()` still follows it and treats the pointed-to
        # `*.local.yaml`/`*.local.json` files as effective configuration. Silently returning an
        # empty snapshot here (the old behavior) made `_verify_local_override_snapshot()` compare
        # `()` to `()` forever, so the tamper guard could never safe-stop even if a Maker edited
        # those files through the worktree. Fail closed instead: this is an infrastructure error
        # (`LocalOverrideSnapshotError`), not a `maker_partial_worktree` safety-stop, since a
        # symlinked root can be present before any Maker activity at all.
        raise LocalOverrideSnapshotError(
            "project-local configuration root (.claude/config) is a symlink; refusing to snapshot"
        )
    if not root.is_dir():
        return ()

    def raise_walk_error(error: OSError) -> None:
        raise error

    targets: list[Path] = []
    try:
        for directory, dirnames, filenames in os.walk(
            root,
            topdown=True,
            followlinks=False,
            onerror=raise_walk_error,
        ):
            for dirname in dirnames:
                # Codex review, PR #262, P2 (round 8): `followlinks=False` above keeps this
                # walk from descending into a symlinked subdirectory, but that just means any
                # `.local.yaml`/`.local.json` override sitting inside one is silently invisible
                # to this snapshot instead -- neither this run's nor any later run's tamper
                # check ever sees it, so a Maker that swaps an intermediate directory under
                # `.claude/config` for a symlink to elsewhere can add, remove, or edit overrides
                # inside it with zero detection signal. Mirrors the round 5 fail-closed
                # treatment of a symlinked config *root* above, one level deeper: any symlinked
                # directory anywhere under the root aborts the snapshot instead of silently
                # skipping it.
                if (Path(directory) / dirname).is_symlink():
                    raise LocalOverrideSnapshotError(
                        "project-local configuration directory is a symlink; refusing to "
                        f"snapshot: {(Path(directory) / dirname).relative_to(worktree_path)}"
                    )
            for name in (*dirnames, *filenames):
                if not name.endswith(_LOCAL_OVERRIDE_SUFFIXES):
                    continue
                targets.append(Path(directory) / name)
        snapshot_paths = set(targets)
        for target in targets:
            ancestor = target.parent
            while True:
                snapshot_paths.add(ancestor)
                if ancestor == worktree_path:
                    break
                if worktree_path not in ancestor.parents:
                    raise LocalOverrideSnapshotError("project-local override escaped the worktree")
                ancestor = ancestor.parent
        snapshots = [_snapshot_entry(worktree_path, path) for path in snapshot_paths]
    except OSError as exc:
        raise LocalOverrideSnapshotError(
            "could not snapshot project-local configuration overrides"
        ) from exc
    return tuple(sorted(snapshots, key=lambda item: item.path))


def changed_local_override_paths(
    expected: tuple[LocalOverrideSnapshot, ...],
    actual: tuple[LocalOverrideSnapshot, ...],
) -> list[str]:
    """Return only changed paths; snapshot digests and contents stay out of diagnostics."""
    expected_by_path = {item.path: item for item in expected}
    actual_by_path = {item.path: item for item in actual}
    return sorted(
        path
        for path in expected_by_path.keys() | actual_by_path.keys()
        if expected_by_path.get(path) != actual_by_path.get(path)
    )


def _resolved_symlink_target_digest(path: Path) -> bytes:
    """Hash a symlinked override's target only when it is a regular file.

    Codex review, PR #262, P2 (round 9): the previous unconditional `path.read_bytes()` follows
    the symlink and opens whatever it points at -- if a Maker repoints a `.local.yaml`/
    `.local.json` override at a FIFO (or other blocking special file) before the post-action
    snapshot, that call blocks the loop driver indefinitely instead of producing the intended
    fail-closed `maker_partial_worktree` safe-stop. `Path.stat()` follows the symlink via the
    `stat(2)` syscall without opening the target, so its type can be checked first; only a
    regular file is actually opened and read.

    PR #262 push-front adversarial review, P2 (round 10): the round-9 fix then capped that read
    at a fixed 10 MiB (`handle.read(_MAX_SYMLINK_TARGET_READ_BYTES)`), so tampering located past
    that cutoff in a larger target never reached the digest at all -- a Maker (or anything else
    writing to the worktree between snapshots) could edit only the tail of a >10 MiB target and
    have it go completely undetected, and the snapshot itself carries no size field to catch the
    truncation independently. This streams the whole file through SHA-256 in fixed-size chunks
    instead (`_SYMLINK_TARGET_HASH_CHUNK_BYTES` bounds memory per iteration, not total bytes
    read), so there is no longer any size past which a tampered target is invisible to this
    guard; only the final digest (32 bytes) is retained, never the file's raw content. Any other
    case (missing target, directory, device/FIFO/socket, permission denied) reports no
    resolved-content digest (`b""`), matching the existing "unreadable target" fallback: this
    guard treats those only as a link-path-change tampering signal, not as unresolvable content
    of their own. `b""` never collides with a real digest (even a zero-byte regular file hashes
    to a non-empty 32-byte SHA-256 digest), so the fallback stays unambiguous.
    """
    try:
        target_metadata = path.stat()
    except OSError:
        return b""
    if not stat.S_ISREG(target_metadata.st_mode):
        return b""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_SYMLINK_TARGET_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return b""
    return digest.digest()


def _snapshot_entry(worktree_path: Path, path: Path) -> LocalOverrideSnapshot:
    relative = path.relative_to(worktree_path).as_posix()
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        kind = "symlink"
        # Codex review, PR #262, High (round 2): hashing only the link text let a Maker change
        # the *contents* of a symlinked `.local.yaml`/`.local.json` override's target -- the
        # effective, currently-loaded override -- without changing the symlink path itself, so
        # `_verify_local_override_snapshot()` saw no delta. Combining the link text with the
        # resolved target's digest (`_resolved_symlink_target_digest()` below) catches either
        # the link being repointed or the target being edited in place. A target that cannot be
        # read, is not a regular file, or is missing/a directory still falls back to detecting
        # only link-path changes rather than raising or blocking, since none of those on their
        # own is a content-tampering signal this guard needs to stop.
        link_target = os.readlink(path).encode()
        resolved_digest = _resolved_symlink_target_digest(path)
        material = link_target + b"\0" + resolved_digest
    elif stat.S_ISREG(metadata.st_mode):
        kind = "file"
        material = path.read_bytes()
    elif stat.S_ISDIR(metadata.st_mode):
        kind = "directory"
        material = b""
    else:
        kind = "other"
        material = str(stat.S_IFMT(metadata.st_mode)).encode()
    digest = hashlib.sha256(kind.encode() + b"\0" + material).hexdigest()
    # Codex review, PR #262, High: a directory's `link_count` increases/decreases whenever *any*
    # direct child subdirectory is created/removed (each child adds a `..` hardlink back to its
    # parent), including ancestor directories walked up to `worktree_path` itself (the repo
    # root). A normal Maker change that creates an unrelated new top-level directory therefore
    # changed the worktree root's snapshot and made `_verify_local_override_snapshot()` safe-stop
    # as `maker_partial_worktree` even though no local override changed. `link_count` is only a
    # meaningful tamper signal for the override *files* themselves (see the "hardlink"
    # regression test, which swaps a `.local.yaml` for a hardlinked alias with identical bytes --
    # `link_count` is what catches that); it is not meaningful for directories, where it reflects
    # unrelated sibling activity rather than tampering with the override tree itself. Pin it to a
    # constant for directory-kind entries so mode/uid/gid/device/inode tampering on the ancestor
    # directories (still exercised by the "*_mode" regression tests) remains caught.
    link_count = 0 if kind == "directory" else metadata.st_nlink
    return LocalOverrideSnapshot(
        path=relative,
        kind=kind,
        mode=stat.S_IMODE(metadata.st_mode),
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        link_count=link_count,
        digest=digest,
    )
