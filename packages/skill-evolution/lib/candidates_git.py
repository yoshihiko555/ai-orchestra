"""コミットの通常 blob から hook と共通のスキル版を計算する。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import candidates_scenarios as scenarios
import skill_evolution_common as common
from candidates_handoff_events import commit_id, require_pattern
from candidates_safety import ValidationError


def tree_entries(
    directory: Path, commit: str, path: str
) -> Iterator[tuple[str, str, str, str, int | None]]:
    if commit != "HEAD":
        commit_id(commit)
    listing = scenarios.git_read(
        directory, "ls-tree", "-r", "-z", "-l", commit, "--", path, binary=True
    ).stdout
    try:
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            metadata, filename = entry.split(b"\t", 1)
            mode, kind, sha, raw_size = metadata.decode("ascii").split()
            commit_id(sha)
            size = None if raw_size == "-" else int(raw_size)
            if (kind == "blob" and size is None) or (size is not None and size < 0):
                raise ValueError
            yield mode, kind, sha, filename.decode("utf-8"), size
    except (ValueError, UnicodeError) as exc:
        raise ValidationError("git ツリーの形式が正しくありません") from exc


def skill_tree_hash(directory: Path, slug: str, commit: str, config: dict) -> str | None:
    require_pattern(slug, r"[a-z0-9-]+", "スキルの slug")
    try:
        settings = config.get("runs") or {}
        max_files = int(settings.get("hash_max_files", common.DEFAULTS["runs"]["hash_max_files"]))
        max_bytes = int(settings.get("hash_max_bytes", common.DEFAULTS["runs"]["hash_max_bytes"]))
    except (AttributeError, TypeError, ValueError):
        return None
    if max_files < 0 or max_bytes < 0:
        return None
    prefix = f".claude/skills/{slug}/"
    entries = list(tree_entries(directory, commit, prefix))
    if not entries:
        return None

    selected: list[tuple[str, str]] = []
    byte_count = 0
    for mode, kind, sha, path, size in entries:
        if mode not in {"100644", "100755"} or kind != "blob":
            continue
        if not path.startswith(prefix):
            raise ValidationError("git ツリーのスキルのパスが正しくありません")
        relative = path.removeprefix(prefix)
        if common._excluded_skill_path(PurePosixPath(relative)):
            continue
        if size is None:
            raise ValidationError("git ツリーの blob のサイズがありません")
        byte_count += size
        selected.append((relative, sha))
        if len(selected) > max_files or byte_count > max_bytes:
            return None
    blobs = (
        (relative, scenarios.git_read(directory, "cat-file", "blob", sha, binary=True).stdout)
        for relative, sha in selected
    )
    return common.hash_skill_files(blobs, max_files=max_files, max_bytes=max_bytes)
