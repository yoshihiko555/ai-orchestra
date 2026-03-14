#!/usr/bin/env python3
"""PreToolUse(Task) hook: サブエージェント起動前に共有コンテキストを prompt に注入する。

処理フロー:
1. stdin から PreToolUse JSON を読み込む
2. tool_name が "Task" でなければ何もしない
3. context_store からセッションエントリーと working-context を取得する
4. どちらも空なら何もしない
5. 注入テキストを構築して tool_input.prompt の末尾に追加する
6. 変更後の tool_input を含む JSON を stdout に出力する
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

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
    from context_store import get_project_dir, read_entries, read_working_context

    _CONTEXT_STORE_AVAILABLE = True
except ImportError:
    _CONTEXT_STORE_AVAILABLE = False

# 注入するエントリーの最大件数（最新 N 件）
_MAX_ENTRIES = 5
# 各エントリーの summary トランケート文字数
_SUMMARY_TRUNCATE = 200
# modified_files の最大表示件数
_MAX_MODIFIED_FILES = 20


def _truncate(text: str, max_chars: int) -> str:
    """文字列を指定文字数にトランケートする。"""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def build_entries_section(entries: list[dict[str, Any]]) -> str:
    """セッションエントリーから注入テキストのセクションを構築する。

    最新 _MAX_ENTRIES 件のエントリーを使用する。summary は _SUMMARY_TRUNCATE 文字にトランケートする。
    """
    if not entries:
        return ""

    # タイムスタンプでソートして最新 N 件を取得
    sorted_entries = sorted(
        entries,
        key=lambda e: e.get("timestamp") or "",
        reverse=True,
    )
    recent = sorted_entries[:_MAX_ENTRIES]
    # 古い順に並び直して表示する
    recent = list(reversed(recent))

    lines = ["## Previous Agent Results"]
    for entry in recent:
        agent_id = entry.get("agent_id") or "unknown"
        task_name = entry.get("task_name") or ""
        summary = entry.get("summary") or ""
        truncated = _truncate(summary, _SUMMARY_TRUNCATE)
        lines.append(f"- {agent_id} ({task_name}): {truncated}")

    return "\n".join(lines)


def build_working_context_section(working_ctx: dict[str, Any]) -> str:
    """working-context から注入テキストのセクションを構築する。

    modified_files は最新 _MAX_MODIFIED_FILES 件に制限する。
    """
    if not working_ctx:
        return ""

    lines = ["## Working Context"]

    modified_files: list[str] = working_ctx.get("modified_files") or []
    if isinstance(modified_files, list) and modified_files:
        limited = modified_files[-_MAX_MODIFIED_FILES:]
        lines.append(f"- Modified files: {', '.join(limited)}")

    current_phase = working_ctx.get("current_phase") or ""
    if current_phase:
        lines.append(f"- Current phase: {current_phase}")

    recent_decisions = working_ctx.get("recent_decisions") or ""
    if recent_decisions:
        lines.append(f"- Recent decisions: {recent_decisions}")

    # modified_files と既定フィールド以外の追加フィールドも出力する
    known_keys = {"modified_files", "current_phase", "recent_decisions", "updated_at"}
    for key, value in working_ctx.items():
        if key in known_keys:
            continue
        if value:
            lines.append(f"- {key}: {value}")

    # セクション本文が "## Working Context" のみなら空扱いにする
    if len(lines) == 1:
        return ""

    return "\n".join(lines)


def build_injection_text(
    entries: list[dict[str, Any]],
    working_ctx: dict[str, Any],
) -> str:
    """注入テキスト全体を構築する。

    エントリーと working-context の両方が空なら空文字を返す。
    """
    sections: list[str] = []

    entries_section = build_entries_section(entries)
    if entries_section:
        sections.append(entries_section)

    ctx_section = build_working_context_section(working_ctx)
    if ctx_section:
        sections.append(ctx_section)

    if not sections:
        return ""

    body = "\n\n".join(sections)
    return f"\n\n[Shared Context]\n{body}"


@safe_hook_execution
def main() -> None:
    """PreToolUse(Task) hook のエントリポイント。"""
    if not _CONTEXT_STORE_AVAILABLE:
        return

    try:
        raw = sys.stdin.read()
        data: dict = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return

    # Task ツール以外は何もしない
    tool_name = data.get("tool_name") or ""
    if tool_name != "Task":
        return

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return

    project_dir = get_project_dir(data)

    entries = read_entries(project_dir)
    working_ctx = read_working_context(project_dir)

    injection = build_injection_text(entries, working_ctx)
    if not injection:
        return

    original_prompt = tool_input.get("prompt") or ""
    new_prompt = original_prompt + injection

    new_tool_input = {**tool_input, "prompt": new_prompt}

    output = {
        "decision": "approve",
        "tool_input": new_tool_input,
    }
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
