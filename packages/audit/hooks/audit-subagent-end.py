#!/usr/bin/env python3
"""SubagentStop hook: サブエージェント終了を記録する。"""

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

from event_logger import cleanup_subagent_trace, emit_event, load_subagent_trace
from hook_common import get_field, read_hook_input, safe_hook_execution


@safe_hook_execution
def main() -> None:
    data = read_hook_input()

    session_id = get_field(data, "session_id")
    agent_id = get_field(data, "agent_id")
    agent_type = get_field(data, "agent_type") or ""

    if not agent_id or not session_id:
        return

    cwd = get_field(data, "cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    # サブエージェント固有のトレース情報を復元
    sub_trace = load_subagent_trace(agent_id, project_dir=cwd)
    sub_tid = sub_trace.get("tid", "")
    ptid = sub_trace.get("ptid") or None

    # success / result_summary は SubagentStop hook の入力に含まれないため null
    # 将来的に transcript 解析で補完する
    emit_event(
        "subagent_end",
        {
            "agent_type": agent_type,
            "success": None,
            "error_type": None,
            "error_summary": None,
            "duration_ms": None,
            "result_summary": None,
        },
        session_id=session_id,
        tid=sub_tid,
        ptid=ptid,
        aid=agent_id,
        project_dir=cwd,
    )

    cleanup_subagent_trace(agent_id, project_dir=cwd)


if __name__ == "__main__":
    main()
