#!/usr/bin/env python3
"""PostToolUse(Edit|Write) hook: ファイル編集時に変更ファイルリストを working-context.json に追記する。

処理フロー:
1. stdin から PostToolUse JSON を読み込む
2. tool_name が "Edit" でも "Write" でもなければ何もしない
3. tool_input.file_path からファイルパスを取得する
4. project_dir を解決して相対パスに変換する
5. プロジェクト外のパスは記録しない
6. .claude/ 配下のファイルは除外する（自己参照防止）
7. context_store.update_working_context() で modified_files に追加する
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# sys.path に packages/core/hooks を追加
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOK_DIR not in sys.path:
    sys.path.insert(0, _HOOK_DIR)

try:
    from hook_common import read_hook_input, safe_hook_execution
except ImportError:
    import functools
    import json
    from collections.abc import Callable

    def read_hook_input() -> dict:  # type: ignore[misc]
        """stdin から JSON を読み取って dict を返す。"""
        try:
            return json.loads(sys.stdin.read())
        except (json.JSONDecodeError, ValueError):
            return {}

    def safe_hook_execution(func: Callable[[], None]) -> Callable[[], None]:  # type: ignore[misc]
        """フォールバック: 例外時は stderr にログ出力して exit(0) する。"""

        @functools.wraps(func)
        def wrapper() -> None:
            try:
                func()
            except Exception as e:
                print(f"Hook error: {e}", file=sys.stderr)
                sys.exit(0)

        return wrapper


try:
    from context_store import get_project_dir, update_working_context

    _CONTEXT_STORE_AVAILABLE = True
except ImportError:
    from typing import Any

    _CONTEXT_STORE_AVAILABLE = False

    def get_project_dir(data: dict) -> str:  # type: ignore[misc]
        """hook 入力からプロジェクトディレクトリを取得する。"""
        cwd = data.get("cwd") or ""
        if cwd:
            return cwd
        return os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    def update_working_context(project_dir: str, updates: dict[str, Any]) -> None:
        """フォールバック: context_store が利用不可なら何もしない。"""


def to_relative_path(file_path: str, project_dir: str) -> str:
    """file_path から project_dir プレフィックスを除去して相対パスに変換する。

    project_dir が含まれない場合は file_path をそのまま返す。

    file_path をそのまま（変換前の原文のまま）返すのは次の 3 通りのみ:
    (1) project_dir が空、(2) resolve() が例外を送出、(3) 解決後のパスが
    project_dir 配下にない。(1)(2) は絶対パスとは限らないが、(1)(2)(3) の
    いずれであっても `is_outside_project()` はプロジェクト外として除外する
    （安全側に倒す保守的な仕様）。
    """
    if not project_dir:
        return file_path

    try:
        project_root = Path(project_dir).resolve()
        source_path = Path(file_path)
        if source_path.is_absolute():
            resolved_source = source_path.resolve()
        else:
            resolved_source = (project_root / source_path).resolve()

        if resolved_source.is_relative_to(project_root):
            return str(resolved_source.relative_to(project_root))
    except (OSError, RuntimeError, ValueError):
        return file_path

    return file_path


def is_outside_project(relative_path: str) -> bool:
    """to_relative_path() の戻り値がプロジェクト外のパスかどうかを判定する。

    to_relative_path() は project_dir 配下に解決できない場合、file_path を
    そのまま返す契約になっている（`relative_to()` が返す相対パスには `..` は
    含まれない性質を利用する）。この関数はその戻り値を受け取り、次のいずれか
    であればプロジェクト外と判定する:
    - 絶対パスのまま（project_dir 空 / resolve() 例外 / 解決不能）
    - `..` を含む相対パス（`../other/foo.py` のように project_dir の外側へ
      抜けようとする表記）
    """
    path = Path(relative_path)
    return path.is_absolute() or ".." in path.parts


def is_claude_internal(relative_path: str) -> bool:
    """.claude/ 配下のパスかどうかを判定する。

    自己参照を防ぐため、.claude/ 以下のファイル変更は除外する。
    """
    normalized = relative_path.replace("\\", "/")
    return normalized.startswith(".claude/") or normalized == ".claude"


@safe_hook_execution
def main() -> None:
    """PostToolUse(Edit|Write) hook のエントリポイント。"""
    if not _CONTEXT_STORE_AVAILABLE:
        print("update-working-context: context_store not available, skipping.", file=sys.stderr)
        return

    data = read_hook_input()

    # Edit / Write ツール以外は何もしない
    tool_name = data.get("tool_name") or ""
    if tool_name not in ("Edit", "Write"):
        return

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return

    file_path = tool_input.get("file_path") or ""
    if not file_path:
        return

    project_dir = get_project_dir(data)
    relative_path = to_relative_path(file_path, project_dir)
    if is_outside_project(relative_path):
        return

    # .claude/ 配下のファイルは除外する
    if is_claude_internal(relative_path):
        return

    update_working_context(project_dir, {"modified_files": [relative_path]})


if __name__ == "__main__":
    main()
