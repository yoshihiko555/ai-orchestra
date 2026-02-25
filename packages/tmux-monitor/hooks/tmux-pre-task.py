#!/usr/bin/env python3
"""PreToolUse hook: Task ツールの description を一時保存する。

SubagentStart hook でペインタイトルに description を表示するため、
Task 実行前に description をキューに保存する。
"""

import fcntl
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tmux_common import SESSION_INFO_DIR, is_tmux_monitoring_enabled


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    cwd = data.get("cwd", "")
    if not cwd or not is_tmux_monitoring_enabled(cwd):
        sys.exit(0)

    session_id = data.get("session_id", "")
    tool_input = data.get("tool_input", {})
    description = tool_input.get("description", "")

    if not session_id or not description:
        sys.exit(0)

    os.makedirs(SESSION_INFO_DIR, exist_ok=True)
    queue_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.task-queue")

    entry = json.dumps({"description": description})
    try:
        with open(queue_file, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(entry + "\n")
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
