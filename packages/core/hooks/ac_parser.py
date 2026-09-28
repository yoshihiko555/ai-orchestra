#!/usr/bin/env python3
"""Plans.md の Acceptance Criteria (AC) 解析ロジックを提供する共有モジュール。

`load-task-state.py`（SessionStart hook）から切り出した、AC セクション見出しの定義と
AC チェックボックス行の分類を行う。`load-task-state.py` はこれを使って AC 行をタスク
サマリーから除外する。Plans.md の AC 行を扱いたい他コンポーネントは本モジュールを
import して使うこと（architecture-reviewer Medium 指摘, Issue #299）。

設計方針:
    - `verify:` / `judge:` の構文区別はパーサレベルでは扱わない。両者とも
      `- [ ]` / `- [x]` の外形は同一であり、区別を導入すると複雑さが増すだけで恩恵がない
      （coding-principles.md のシンプルさ優先）。
    - SessionStart の自動アーカイブを廃止したため（ADR-20260928-058）、フェーズの完了判定
      （AC セクションの行範囲抽出・未チェック AC の有無判定）は持たない。

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
