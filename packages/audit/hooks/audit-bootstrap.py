#!/usr/bin/env python3
"""SessionStart hook: セッション用ログディレクトリを初期化し session_start を記録する。"""

from __future__ import annotations

import json
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

from event_logger import emit_event, generate_id, init_session_dir, save_trace_state
from hook_common import read_hook_input, safe_hook_execution


@safe_hook_execution
def main() -> None:
    data = read_hook_input()
    session_id = str(data.get("session_id") or "")
    if not session_id:
        return

    cwd = str(data.get("cwd") or "") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    init_session_dir(session_id, project_dir=cwd)

    # セッション開始時にトレース ID を生成して保存
    # 後続 hook が load_trace_state() で参照する
    initial_tid = generate_id()
    save_trace_state(initial_tid, session_id=session_id, project_dir=cwd)

    packages: list[str] = []
    orchestra_path = os.path.join(cwd, ".claude", "orchestra.json")
    if os.path.exists(orchestra_path):
        try:
            with open(orchestra_path) as f:
                orchestra = json.load(f)
            packages = orchestra.get("installed_packages", [])
        except (json.JSONDecodeError, OSError):
            pass

    emit_event(
        "session_start",
        {"packages": packages},
        session_id=session_id,
        tid=initial_tid,
        project_dir=cwd,
    )


if __name__ == "__main__":
    main()
