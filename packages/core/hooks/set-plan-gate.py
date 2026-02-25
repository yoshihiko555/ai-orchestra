#!/usr/bin/env python3
"""PostToolUse hook: plan/planner エージェント完了後に plan gate を設定する。

plan gate が設定されると、ユーザーが次のメッセージを送るまで
実装系エージェントの呼び出しがブロックされる。
"""

from __future__ import annotations

import datetime
import json
import os
import sys

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

from hook_common import safe_hook_execution, write_json  # noqa: E402

# plan gate を設定するエージェント（subagent_type の完全一致のみ）
PLAN_AGENTS: set[str] = {"plan", "planner"}


def _get_state_dir(data: dict) -> str:
    """プロジェクトの .claude/state ディレクトリのパスを返す。"""
    cwd = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not cwd:
        return ""
    return os.path.join(cwd, ".claude", "state")


@safe_hook_execution
def main() -> None:
    data = json.load(sys.stdin)

    # Task ツール以外は無視
    if data.get("tool_name") != "Task":
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    subagent_type = tool_input.get("subagent_type", "").lower()

    # plan エージェントでなければ無視（subagent_type の完全一致のみ）
    if subagent_type not in PLAN_AGENTS:
        sys.exit(0)

    # Task が失敗した場合はゲートを設定しない
    # - None / 空文字: レスポンスなし（失敗）
    # - dict で error が truthy: 構造的エラー
    # - dict で exit_code が非0: プロセスエラー
    tool_response = data.get("tool_response")
    if tool_response is None or tool_response == "":
        sys.exit(0)
    if isinstance(tool_response, dict):
        if tool_response.get("error"):
            sys.exit(0)
        if tool_response.get("exit_code", 0) != 0:
            sys.exit(0)

    # plan gate を設定
    state_dir = _get_state_dir(data)
    if not state_dir:
        sys.exit(0)

    os.makedirs(state_dir, exist_ok=True)
    gate_path = os.path.join(state_dir, "plan-gate.json")

    gate_data = {
        "pending": True,
        "agent": tool_input.get("subagent_type", "unknown"),
        "set_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),  # noqa: UP017
    }
    write_json(gate_path, gate_data)

    # オーケストレーターへの通知
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                "[Plan Gate] 計画が作成されました。"
                "実装エージェントを呼び出す前に、計画をユーザーに提示して承認を得てください。"
                "\nユーザーが次のメッセージを送信するまで、実装系エージェントの呼び出しはブロックされます。"
            ),
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
