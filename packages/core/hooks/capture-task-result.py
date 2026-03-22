#!/usr/bin/env python3
"""PostToolUse(Task) hook: サブエージェント完了時に結果サマリーを書き出す。

処理フロー:
1. stdin から PostToolUse JSON を読み込む
2. tool_name が "Agent"（または後方互換の "Task"）でなければ何もしない
3. tool_input から agent_id / task_name を取得する
4. tool_response を先頭 2000 文字にトランケートしてサマリーとする
5. context_store.write_entry() でエントリーを書き出す
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

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
    from context_store import get_project_dir, write_entry

    _CONTEXT_STORE_AVAILABLE = True
except ImportError:
    _CONTEXT_STORE_AVAILABLE = False

# サマリーの最大文字数（約 500 トークン相当）
_SUMMARY_MAX_CHARS = 2000


def _read_stdin_json() -> dict:
    """stdin から JSON を読み込んで dict を返す。失敗時は空辞書を返す。"""
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return {}


def extract_agent_id(tool_input: dict) -> str:
    """tool_input から agent_id を取得する。なければ "unknown" を返す。"""
    agent_id = tool_input.get("subagent_type") or ""
    return agent_id if agent_id else "unknown"


def extract_task_name(tool_input: dict) -> str:
    """tool_input から task_name を取得する。

    description があればそれを使い、なければ prompt の先頭 50 文字を使う。
    """
    description = tool_input.get("description") or ""
    if description:
        return description

    prompt = tool_input.get("prompt") or ""
    return prompt[:50]


def truncate_summary(text: str) -> str:
    """tool_response を先頭 _SUMMARY_MAX_CHARS 文字にトランケートする。"""
    if not isinstance(text, str):
        text = str(text)
    return text[:_SUMMARY_MAX_CHARS]


def now_iso8601() -> str:
    """現在時刻を ISO8601 形式で返す。"""
    return datetime.now(tz=UTC).isoformat()


@safe_hook_execution
def main() -> None:
    """PostToolUse(Task) hook のエントリポイント。"""
    if not _CONTEXT_STORE_AVAILABLE:
        print("capture-task-result: context_store not available, skipping.", file=sys.stderr)
        return

    data = _read_stdin_json()

    # Agent ツール以外は何もしない（後方互換のため "Task" も許容）
    tool_name = data.get("tool_name") or ""
    if tool_name not in ("Agent", "Task"):
        return

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    tool_response = data.get("tool_response") or ""

    agent_id = extract_agent_id(tool_input)
    task_name = extract_task_name(tool_input)
    summary = truncate_summary(tool_response)
    project_dir = get_project_dir(data)

    entry: dict = {
        "agent_id": agent_id,
        "task_name": task_name,
        "timestamp": now_iso8601(),
        "status": "done",
        "summary": summary,
    }

    write_entry(project_dir, agent_id, entry)


if __name__ == "__main__":
    main()
