#!/usr/bin/env python3
"""plan ファイルの Acceptance Criteria (AC) 解析ロジックを提供する共有モジュール。

`load-task-state.py`（SessionStart hook）から切り出した、AC セクション見出しの定義と
AC チェックボックス行の分類を行う。`load-task-state.py` はこれを使って AC 行をタスク
サマリーから除外する。plan ファイルの AC 行を扱いたい他コンポーネントは本モジュールを
import して使うこと（architecture-reviewer Medium 指摘, Issue #299）。

設計方針:
    - `verify:` / `judge:` の構文区別はパーサレベルでは扱わない。両者とも
      `- [ ]` / `- [x]` の外形は同一であり、区別を導入すると複雑さが増すだけで恩恵がない
      （coding-principles.md のシンプルさ優先）。
    - AC の行範囲はタスク除外とファイル完了判定で共有する（ADR-20261027-059）。
      frontmatter・コードフェンス・HTML コメントの除外行は呼び出し側から渡す。

hooks/ 配下以外（他パッケージのスクリプト等）から import する場合は、
load-task-state.py と同じ sys.path 挿入パターンを使う:

    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[N] / "packages/core/hooks"))
    import ac_parser  # noqa: E402
"""

from __future__ import annotations

import re

__all__ = [
    "AC_SECTION_HEADING",
    "CHECKBOX_PATTERN",
    "classify_checkbox_line",
    "ac_checkbox_line_indices",
    "has_unchecked_ac",
]

# Acceptance Criteria チェックボックス行の判定パターン（`- [ ]` / `- [x]` / `- [X]`）
CHECKBOX_PATTERN = re.compile(r"^- \[([ xX])\]")
# Acceptance Criteria セクションの見出し（strip 後の完全一致で判定する）
AC_SECTION_HEADING = "#### Acceptance Criteria"
_HEADING_PATTERN = re.compile(r"^(#{1,6})(?:\s|$)")


def ac_checkbox_line_indices(lines: list[str], excluded: set[int]) -> set[int]:
    """Phase 配下の AC 節にあるチェックボックス行番号を返す。"""
    indices: set[int] = set()
    in_phase = False
    in_ac_section = False
    for index, line in enumerate(lines):
        if index in excluded:
            continue
        stripped = line.strip()
        heading = _HEADING_PATTERN.match(stripped)
        if heading:
            level = len(heading.group(1))
            if level <= 3:
                in_phase = level == 3
            if level <= 4:
                in_ac_section = in_phase and stripped == AC_SECTION_HEADING
            continue
        if in_ac_section and classify_checkbox_line(stripped) is not None:
            indices.add(index)
    return indices


def has_unchecked_ac(lines: list[str], excluded: set[int]) -> bool:
    """構造解析の除外行を除き、Phase 配下に未チェックの AC があるか返す。"""
    return any(
        classify_checkbox_line(lines[index].strip()) == "unchecked"
        for index in ac_checkbox_line_indices(lines, excluded)
    )


def classify_checkbox_line(stripped: str) -> str | None:
    """チェックボックス行（`- [ ]` / `- [x]` / `- [X]`）を分類する。

    Markdown リンク箇条書き（例: `- [text](url)`）を誤って "checked" 扱いしないよう、
    厳密な正規表現でチェックボックス行かどうかを判定する。

    Args:
        stripped: strip 済みの行文字列。

    Returns:
        "unchecked" | "checked" | None（チェックボックス行でない場合）
    """
    match = CHECKBOX_PATTERN.match(stripped)
    if not match:
        return None
    return "unchecked" if match.group(1) == " " else "checked"
