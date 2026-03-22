"""AI Orchestra の .gitignore ブロック管理。

OrchestraManager (orchestra-manager.py) と SessionStart hook (sync-orchestra.py)
の両方から共通で使用する。
"""

from __future__ import annotations

import re
from pathlib import Path

BLOCK_START = "# >>> AI Orchestra (.claude) >>>"
BLOCK_END = "# <<< AI Orchestra (.claude) <<<"
ENTRIES = [
    ".claude/docs/",
    ".claude/logs/",
    ".claude/state/",
    ".claude/checkpoints/",
    ".claude/context/",
    ".claude/.facet-packages-hash",
    ".claude/.facet-manifest.json",
    ".claude/Plans.md",
    ".claude/Plans.archive.md",
]


def build_block() -> str:
    """AI Orchestra 管理下の .gitignore ブロックを返す。"""
    lines = [BLOCK_START, *ENTRIES, BLOCK_END]
    return "\n".join(lines) + "\n"


def merge_content(existing: str) -> str:
    """既存 .gitignore 文字列に AI Orchestra ブロックをマージする。"""
    block = build_block()

    # 既存ブロックの置換
    start_idx = existing.find(BLOCK_START)
    end_idx = existing.find(BLOCK_END)
    if start_idx >= 0 and end_idx >= 0 and start_idx < end_idx:
        end_idx += len(BLOCK_END)
        before = existing[:start_idx].rstrip("\n")
        after = existing[end_idx:].lstrip("\n")
        parts = []
        if before:
            parts.append(before)
        parts.append(block.rstrip("\n"))
        if after:
            parts.append(after.rstrip("\n"))
        return "\n\n".join(parts) + "\n"

    # 既存で同等エントリがすべてある場合は追記しない
    all_exist = all(re.search(rf"(?m)^{re.escape(e)}$", existing) is not None for e in ENTRIES)
    if all_exist:
        return existing if existing.endswith("\n") else existing + "\n"

    if not existing.strip():
        return block

    base = existing if existing.endswith("\n") else existing + "\n"
    return base + "\n" + block


def sync_gitignore(project_dir: Path) -> bool:
    """プロジェクトの .gitignore に AI Orchestra ブロックを追加/更新する。"""
    path = project_dir / ".gitignore"
    existing = ""
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = ""

    merged = merge_content(existing)
    if merged == existing:
        return False

    try:
        path.write_text(merged, encoding="utf-8")
    except OSError:
        return False
    return True
