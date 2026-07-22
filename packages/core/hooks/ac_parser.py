#!/usr/bin/env python3
"""Plans.md の Acceptance Criteria (AC) 解析ロジックを提供する共有モジュール。

`load-task-state.py`（SessionStart hook）から切り出した、AC チェックボックス行の
分類・AC セクションの行範囲抽出・未チェック AC の有無判定を行う純粋関数群。
task-state スキル支援ツールなど、Plans.md の AC 判定を再利用したい他コンポーネントは
本モジュールを import して使うこと（architecture-reviewer Medium 指摘, Issue #299）。

設計方針:
    - 完了判定は現状どおり「チェックボックスの有無（unchecked/checked）」のみを見る。
    - `verify:` / `judge:` の構文区別はパーサレベルでは扱わない。両者とも
      `- [ ]` / `- [x]` の外形は同一であり、完了判定（フェーズ完了 = AC 全て `[x]`）に
      種別の違いは影響しないため、区別を導入すると複雑さが増すだけで恩恵がない
      （coding-principles.md のシンプルさ優先）。将来 verify/judge を区別した
      機械検証（例: verify コマンドの自動実行）が必要になった時点で、本モジュールに
      追加のパーサ（例: `classify_ac_kind()`）を足す形が望ましい。

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
    "CHECKBOX_PATTERN",
    "AC_SECTION_HEADING",
    "classify_checkbox_line",
    "ac_section_ranges",
    "phase_has_unchecked_ac",
]

# Acceptance Criteria チェックボックス行の判定パターン（`- [ ]` / `- [x]` / `- [X]`）
CHECKBOX_PATTERN = re.compile(r"^- \[([ xX])\]")
# Acceptance Criteria セクションの見出し（strip 後の完全一致で判定する）
AC_SECTION_HEADING = "#### Acceptance Criteria"


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


def _find_ac_section_end(lines: list[str], start: int, phase_end: int) -> int:
    """AC セクション本文の終端行インデックスを返す（次の `#### ` 見出し手前 or phase_end）。"""
    for i in range(start, phase_end + 1):
        if lines[i].startswith("#### "):
            return i - 1
    return phase_end


def ac_section_ranges(lines: list[str], phase_start: int, phase_end: int) -> list[tuple[int, int]]:
    """フェーズ範囲内にある `#### Acceptance Criteria` セクション本文の行範囲一覧を返す。

    Args:
        lines: Plans.md 全体を splitlines() した行リスト。
        phase_start: フェーズ見出し（`### Phase ...`）の行インデックス。
        phase_end: フェーズの終端行インデックス（次フェーズ直前 or プロジェクト終端）。

    Returns:
        [(section_start, section_end), ...] のリスト（本文が空の場合は空範囲も含む）。
    """
    ranges: list[tuple[int, int]] = []
    i = phase_start + 1
    while i <= phase_end:
        if lines[i].strip() != AC_SECTION_HEADING:
            i += 1
            continue
        section_start = i + 1
        section_end = _find_ac_section_end(lines, section_start, phase_end)
        ranges.append((section_start, section_end))
        i = section_end + 1 if section_end >= section_start else i + 1
    return ranges


def phase_has_unchecked_ac(lines: list[str], ac_ranges: list[tuple[int, int]]) -> bool:
    """AC セクション内に未チェックの Acceptance Criteria 行があるか判定する。"""
    ac_lines = [line for start, end in ac_ranges for line in lines[start : end + 1]]
    return any(classify_checkbox_line(line.strip()) == "unchecked" for line in ac_lines)
