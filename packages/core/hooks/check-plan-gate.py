#!/usr/bin/env python3
"""PreToolUse hook: plan gate 設定中に実装系エージェントの呼び出しをブロックする。

plan gate が pending の状態で実装系エージェントが呼ばれた場合、
exit code 2 でツール呼び出しをブロックし、計画のユーザー承認を促す。
"""

from __future__ import annotations

import json
import os
import sys

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

from hook_common import read_json_safe, safe_hook_execution  # noqa: E402

# 実装系エージェント（plan gate でブロック対象）
IMPLEMENTATION_AGENTS: set[str] = {
    "frontend-dev",
    "backend-python-dev",
    "backend-go-dev",
    "ai-dev",
    "rag-engineer",
    "debugger",
    "tester",
    "spec-writer",
}

# ブロックはしないが警告を出すエージェント（Codex/Gemini 委譲にも使われるため）
WARN_AGENTS: set[str] = {"general-purpose"}


def _get_gate_path(data: dict) -> str:
    """plan-gate.json のパスを返す。"""
    cwd = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not cwd:
        return ""
    return os.path.join(cwd, ".claude", "state", "plan-gate.json")


@safe_hook_execution
def main() -> None:
    data = json.load(sys.stdin)

    # Task ツール以外は無視
    if data.get("tool_name") != "Task":
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    subagent_type = tool_input.get("subagent_type", "").lower()

    # 対象外エージェントは無視
    is_impl = subagent_type in IMPLEMENTATION_AGENTS
    is_warn = subagent_type in WARN_AGENTS
    if not is_impl and not is_warn:
        sys.exit(0)

    # plan gate の状態を確認
    gate_path = _get_gate_path(data)
    if not gate_path:
        sys.exit(0)

    gate = read_json_safe(gate_path)
    if not gate.get("pending", False):
        sys.exit(0)

    plan_agent = gate.get("agent", "planner")

    # general-purpose は警告のみ（Codex/Gemini 委譲を壊さないため）
    if is_warn:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": (
                    f"[Plan Gate Warning] 計画（{plan_agent}）がユーザーに未提示です。\n"
                    f"`{subagent_type}` で実装を行う場合は、先に計画をユーザーに提示してください。"
                ),
            }
        }
        print(json.dumps(output))
        sys.exit(0)

    # 実装系エージェント: ブロック（exit 2）
    message = (
        f"[Plan Gate] 計画（{plan_agent}）がユーザーに未提示です。\n"
        f"実装エージェント `{subagent_type}` の呼び出しをブロックしました。\n\n"
        "次のアクションを実行してください:\n"
        "1. 計画の内容をユーザーに提示する\n"
        "2. ユーザーの承認を待つ\n"
        "3. 承認後に実装エージェントを呼び出す"
    )
    print(message, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
