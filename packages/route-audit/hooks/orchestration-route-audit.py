#!/usr/bin/env python3
"""PostToolUse hook: 実際ルートを記録し、期待ルート一致率を計測する。"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
from typing import Any

_hook_dir = os.path.dirname(os.path.abspath(__file__))

TEST_CMD_PATTERN = re.compile(r"\b(pytest|npm\s+test|pnpm\s+test|yarn\s+test|go\s+test|cargo\s+test|ruff\s+check|mypy)\b")


def read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {}


def write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_jsonl(path: str, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def find_first_text(node: Any, keys: set[str]) -> str:
    if isinstance(node, dict):
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in node.values():
            found = find_first_text(value, keys)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_first_text(item, keys)
            if found:
                return found
    return ""


def find_first_int(node: Any, keys: set[str]) -> int | None:
    if isinstance(node, dict):
        for key in keys:
            value = node.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                try:
                    return int(value)
                except ValueError:
                    pass
        for value in node.values():
            found = find_first_int(value, keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_first_int(item, keys)
            if found is not None:
                return found
    return None


def detect_route(data: dict) -> tuple[str | None, str]:
    tool_name = str(data.get("tool_name") or find_first_text(data, {"tool_name", "tool"}))
    tool_lower = tool_name.lower()
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}

    if tool_lower == "bash":
        command = ""
        if isinstance(tool_input, dict):
            command = str(tool_input.get("command") or tool_input.get("cmd") or "")
        if not command:
            command = find_first_text(data, {"command", "cmd"})

        cmd_lower = command.lower()
        if "codex" in cmd_lower:
            return "bash:codex", command[:200]
        if "gemini" in cmd_lower:
            return "bash:gemini", command[:200]
        return None, command[:200]

    if tool_lower == "task":
        subagent_type = "agent"
        if isinstance(tool_input, dict):
            subagent_type = str(
                tool_input.get("subagent_type")
                or tool_input.get("agent_type")
                or tool_input.get("agent")
                or "agent"
            )
        return f"task:{subagent_type}", ""

    return None, ""


def is_match(expected_route: str, actual_route: str, policy: dict) -> bool:
    if not expected_route or not actual_route:
        return False

    if expected_route == actual_route:
        return True

    aliases = policy.get("aliases") or {}
    if actual_route in (aliases.get(expected_route) or []):
        return True

    if expected_route == "subagent-general-purpose" and actual_route.startswith("task:"):
        return True

    return False


def project_root(data: dict) -> str:
    return (
        data.get("cwd")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.path.abspath(os.path.join(_hook_dir, "..", "..", ".."))
    )


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        data = {}

    root = project_root(data)
    flags = read_json(os.path.join(root, ".claude", "config", "orchestration-flags.yaml"))
    route_audit = ((flags.get("features") or {}).get("route_audit") or {})
    if not route_audit.get("enabled", True):
        sys.exit(0)

    actual_route, command_excerpt = detect_route(data)
    if not actual_route:
        sys.exit(0)

    policy = read_json(os.path.join(root, ".claude", "config", "delegation-policy.yaml"))
    state_dir = os.path.join(root, ".claude", "state")
    logs_dir = os.path.join(root, ".claude", "logs", "orchestration")
    os.makedirs(state_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)

    expected = read_json(os.path.join(state_dir, "expected-route.json"))
    expected_route = str(expected.get("expected_route") or "")
    prompt_id = str(expected.get("prompt_id") or "")
    prompt_excerpt = str(expected.get("prompt_excerpt") or "")

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    matched = is_match(expected_route, actual_route, policy)

    record = {
        "timestamp": now,
        "session_id": str(data.get("session_id") or ""),
        "prompt_id": prompt_id,
        "prompt_excerpt": prompt_excerpt,
        "expected_route": expected_route,
        "actual_route": actual_route,
        "matched": matched,
        "tool_name": str(data.get("tool_name") or find_first_text(data, {"tool_name", "tool"})),
        "command_excerpt": command_excerpt,
    }
    append_jsonl(os.path.join(logs_dir, "route-audit.jsonl"), record)
    append_jsonl(
        os.path.join(logs_dir, "agent-trace.jsonl"),
        {
            "event": "route_audit",
            "timestamp": now,
            "prompt_id": prompt_id,
            "expected_route": expected_route,
            "actual_route": actual_route,
            "matched": matched,
        },
    )

    active_path = os.path.join(state_dir, "active.json")
    active = read_json(active_path)
    active["last_route"] = actual_route
    active["updated_at"] = now
    write_json(active_path, active)

    if str(data.get("tool_name") or "").lower() == "bash" and TEST_CMD_PATTERN.search(command_excerpt):
        tool_response = data.get("tool_response") if isinstance(data.get("tool_response"), dict) else {}
        exit_code = find_first_int(tool_response, {"exit_code", "code", "status"})
        append_jsonl(
            os.path.join(logs_dir, "quality-gate.jsonl"),
            {
                "timestamp": now,
                "session_id": str(data.get("session_id") or ""),
                "prompt_id": prompt_id,
                "command": command_excerpt,
                "exit_code": exit_code,
                "passed": exit_code == 0 if exit_code is not None else None,
            },
        )

    # 統一イベントログ
    try:
        _orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
        if _orchestra_dir:
            _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
            if _core_hooks not in sys.path:
                sys.path.insert(0, _core_hooks)
        from log_common import append_event
        session_id = str(data.get("session_id") or "")
        append_event(
            "route_audit",
            {"expected_route": expected_route, "actual_route": actual_route, "matched": matched, "prompt_id": prompt_id},
            session_id=session_id,
            hook_name="route-audit",
            project_dir=root,
        )
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
