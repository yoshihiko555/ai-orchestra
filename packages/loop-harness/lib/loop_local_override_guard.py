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
        material = os.readlink(path).encode()
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
    return LocalOverrideSnapshot(
        path=relative,
        kind=kind,
        mode=stat.S_IMODE(metadata.st_mode),
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        link_count=metadata.st_nlink,
        digest=digest,
    )
