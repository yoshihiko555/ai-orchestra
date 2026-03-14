#!/usr/bin/env python3
"""SessionEnd hook: セッション終了時にコンテキストストアをクリーンアップする。

処理フロー:
1. stdin から SessionEnd JSON を読み込む
2. project_dir を解決する（cwd → CLAUDE_PROJECT_DIR → os.getcwd()）
3. context_store.cleanup_session() を呼び出す
4. 完了メッセージを stderr に出力する

セッション間記憶は claude-mem に委任するため、ここでは単純に削除するだけ。
クリーンアップ失敗はエラーとせず、常に正常終了する。
"""

from __future__ import annotations

import json
import os
import sys

# sys.path に packages/core/hooks を追加
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOK_DIR not in sys.path:
    sys.path.insert(0, _HOOK_DIR)

try:
    from hook_common import safe_hook_execution
except ImportError:
    import functools
    from collections.abc import Callable

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
    from context_store import cleanup_session, get_project_dir

    _CONTEXT_STORE_AVAILABLE = True
except ImportError:
    _CONTEXT_STORE_AVAILABLE = False

    def get_project_dir(data: dict) -> str:  # type: ignore[misc]
        """hook 入力からプロジェクトディレクトリを取得する。"""
        cwd = data.get("cwd") or ""
        if cwd:
            return cwd
        return os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def _read_stdin_json() -> dict:
    """stdin から JSON を読み込んで dict を返す。失敗時は空辞書を返す。"""
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return {}


@safe_hook_execution
def main() -> None:
    """SessionEnd hook のエントリポイント。"""
    if not _CONTEXT_STORE_AVAILABLE:
        print("cleanup-session-context: context_store not available, skipping.", file=sys.stderr)
        return

    data = _read_stdin_json()
    project_dir = get_project_dir(data)

    cleanup_session(project_dir)

    print("cleanup-session-context: session context cleaned up.", file=sys.stderr)


if __name__ == "__main__":
    main()
