#!/usr/bin/env python3
"""UserPromptSubmit hook: ユーザーの次のメッセージで plan gate を解除する。

ユーザーがメッセージを送信した時点で、計画を確認したとみなし
plan gate を解除して実装エージェントの呼び出しを許可する。
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

from hook_common import safe_hook_execution  # noqa: E402


def _get_gate_path(data: dict) -> str:
    """plan-gate.json のパスを返す。"""
    cwd = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not cwd:
        return ""
    return os.path.join(cwd, ".claude", "state", "plan-gate.json")


@safe_hook_execution
def main() -> None:
    data = json.load(sys.stdin)
    gate_path = _get_gate_path(data)

    if not gate_path or not os.path.isfile(gate_path):
        sys.exit(0)

    # plan gate ファイルを削除してゲートを解除
    try:
        os.remove(gate_path)
    except OSError:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
