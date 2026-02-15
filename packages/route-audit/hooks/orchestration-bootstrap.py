#!/usr/bin/env python3
"""SessionStart 時にオーケストレーション用の状態ファイルを初期化する。"""

from __future__ import annotations

import datetime
import os
import sys

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

from hook_common import get_field, read_hook_input, read_json_safe, write_json  # noqa: E402

_hook_dir = os.path.dirname(os.path.abspath(__file__))


def _touch(path: str) -> None:
    if not os.path.exists(path):
        with open(path, "w"):
            pass


def main() -> None:
    data = read_hook_input()
    cwd = (
        get_field(data, "cwd")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.path.abspath(os.path.join(_hook_dir, "..", "..", ".."))
    )

    state_dir = os.path.join(cwd, ".claude", "state")
    logs_dir = os.path.join(cwd, ".claude", "logs", "orchestration")

    os.makedirs(state_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)

    active_path = os.path.join(state_dir, "active.json")
    active = read_json_safe(active_path)
    active.setdefault("phase", "planning")
    active.setdefault("current_week_goal", "")
    active.setdefault("expected_route", None)
    active.setdefault("last_route", None)
    active.setdefault("last_prompt_excerpt", "")
    active["updated_at"] = datetime.datetime.now(datetime.UTC).isoformat()
    write_json(active_path, active)

    expected_path = os.path.join(state_dir, "expected-route.json")
    if not os.path.exists(expected_path):
        write_json(expected_path, {})

    _touch(os.path.join(state_dir, "agent-trace.jsonl"))
    _touch(os.path.join(logs_dir, "expected-routes.jsonl"))
    _touch(os.path.join(logs_dir, "route-audit.jsonl"))
    _touch(os.path.join(logs_dir, "quality-gate.jsonl"))
    _touch(os.path.join(logs_dir, "events.jsonl"))


if __name__ == "__main__":
    main()
