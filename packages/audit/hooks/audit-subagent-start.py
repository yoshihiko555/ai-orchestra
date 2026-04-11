#!/usr/bin/env python3
"""SubagentStart hook: サブエージェント起動を記録する。"""

from __future__ import annotations

import os
import sys

_hook_dir = os.path.dirname(os.path.abspath(__file__))
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)
    _audit_hooks = os.path.join(_orchestra_dir, "packages", "audit", "hooks")
    if _audit_hooks not in sys.path:
        sys.path.insert(0, _audit_hooks)
else:
    if _hook_dir not in sys.path:
        sys.path.insert(0, _hook_dir)

from event_logger import emit_event, generate_id, load_trace_state, save_subagent_trace
from hook_common import get_field, read_hook_input, safe_hook_execution


@safe_hook_execution
def main() -> None:
    data = read_hook_input()

    session_id = get_field(data, "session_id")
    agent_id = get_field(data, "agent_id")
    agent_type = get_field(data, "agent_type")

    if not agent_id or not session_id:
        return

    cwd = get_field(data, "cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    # 親のトレース ID を取得
    trace = load_trace_state(project_dir=cwd)
    parent_tid = trace.get("tid", "")

    # サブエージェント用の新しいトレース ID
    sub_tid = generate_id()

    save_subagent_trace(aid=agent_id, tid=sub_tid, ptid=parent_tid, project_dir=cwd)

    # task_summary は SubagentStart hook の入力に含まれないため null
    # 将来的に PreToolUse:Task で description をキャプチャする方式に変更可能
    emit_event(
        "subagent_start",
        {
            "agent_type": agent_type,
            "task_summary": None,
        },
        session_id=session_id,
        tid=sub_tid,
        ptid=parent_tid or None,
        aid=agent_id,
        project_dir=cwd,
    )


if __name__ == "__main__":
    main()
