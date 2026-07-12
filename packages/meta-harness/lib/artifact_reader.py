#!/usr/bin/env python3
"""Race-resistant reads of untrusted candidate artifacts."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RegularArtifact:
    path: Path
    size: int
    data: bytes


def read_regular_artifact(
    root: Path,
    path: Path,
    *,
    max_bytes: int,
) -> RegularArtifact | None:
    """Open every path component with O_NOFOLLOW, then fstat and read the same fd."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        return None
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        directory_fd = os.open(root, directory_flags)
        for component in relative.parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative.parts[-1], file_flags, dir_fd=directory_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(file_fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            return None
        return RegularArtifact(path=path, size=info.st_size, data=data)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def glob_regular_artifacts(
    root: Path,
    pattern: str,
    *,
    max_bytes: int,
) -> list[RegularArtifact]:
    """Resolve a trusted glob pattern, rejecting every symlinked path component at open time."""
    return [
        artifact
        for path in root.glob(pattern)
        if (artifact := read_regular_artifact(root, path, max_bytes=max_bytes)) is not None
    ]
