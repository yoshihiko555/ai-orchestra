#!/usr/bin/env python3
"""Content-free snapshots for user-owned project-local configuration overrides."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

_LOCAL_OVERRIDE_SUFFIXES = (".local.yaml", ".local.json")


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
    if root.is_symlink() or not root.is_dir():
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


def _snapshot_entry(worktree_path: Path, path: Path) -> LocalOverrideSnapshot:
    relative = path.relative_to(worktree_path).as_posix()
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        kind = "symlink"
        # Codex review, PR #262, High: hashing only the link text let a Maker change the
        # *contents* of a symlinked `.local.yaml`/`.local.json` override's target -- the
        # effective, currently-loaded override -- without changing the symlink path itself, so
        # `_verify_local_override_snapshot()` saw no delta. `path.read_bytes()` (unlike
        # `os.readlink`) follows the symlink and reads the resolved target's current bytes, the
        # same way the "file" branch below hashes a regular override's content; combining both
        # into the digest catches either the link being repointed or the target being edited in
        # place. A target that cannot be read (missing, a directory, permission denied) still
        # falls back to detecting only link-path changes rather than raising, since a dangling
        # or unreadable symlink is not itself a content-tampering signal this guard needs to stop.
        link_target = os.readlink(path).encode()
        try:
            resolved_material = path.read_bytes()
        except OSError:
            resolved_material = b""
        material = link_target + b"\0" + resolved_material
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
