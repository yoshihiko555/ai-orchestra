#!/usr/bin/env python3
"""SessionStart 時にオーケストレーション用の状態ファイルを初期化する。"""

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

from hook_common import get_field, read_hook_input


def _read_json(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {}


def _write_json(path: str, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _touch(path: str) -> None:
    if not os.path.exists(path):
        with open(path, "w"):
            pass


def main() -> None:
    data = read_hook_input()
    cwd = get_field(data, "cwd") or os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    state_dir = os.path.join(cwd, ".claude", "state")
    logs_dir = os.path.join(cwd, ".claude", "logs", "orchestration")

    os.makedirs(state_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)

    active_path = os.path.join(state_dir, "active.json")
    active = _read_json(active_path)
    active.setdefault("phase", "planning")
    active.setdefault("current_week_goal", "")
    active.setdefault("expected_route", None)
    active.setdefault("last_route", None)
    active.setdefault("last_prompt_excerpt", "")
    active["updated_at"] = datetime.datetime.now(datetime.UTC).isoformat()
    _write_json(active_path, active)

    expected_path = os.path.join(state_dir, "expected-route.json")
    if not os.path.exists(expected_path):
        _write_json(expected_path, {})

    _touch(os.path.join(state_dir, "agent-trace.jsonl"))
    _touch(os.path.join(logs_dir, "expected-routes.jsonl"))
    _touch(os.path.join(logs_dir, "route-audit.jsonl"))
    _touch(os.path.join(logs_dir, "quality-gate.jsonl"))
    _touch(os.path.join(logs_dir, "events.jsonl"))


if __name__ == "__main__":
    main()
