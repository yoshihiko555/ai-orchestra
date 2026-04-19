#!/usr/bin/env python3
"""PostToolUse hook: 実際のルートを検出し route_decision を記録する。"""

from __future__ import annotations

import os
import re
import sys

_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)
    _routing_hooks = os.path.join(_orchestra_dir, "packages", "agent-routing", "hooks")
    if _routing_hooks not in sys.path:
        sys.path.insert(0, _routing_hooks)
    _audit_hooks = os.path.join(_orchestra_dir, "packages", "audit", "hooks")
    if _audit_hooks not in sys.path:
        sys.path.insert(0, _audit_hooks)

from event_logger import (
    emit_event,
    load_trace_state,
    resolve_project_root_from_hook_data,
)
from hook_common import (
    find_first_text,
    load_package_config,
    read_hook_input,
    read_json_safe,
    safe_hook_execution,
)
from route_config import build_aliases, load_config

# ---------------------------------------------------------------------------
# Route detection
# ---------------------------------------------------------------------------


def detect_route(data: dict) -> tuple[str | None, str, str]:
    """ツール呼び出しから実際のルートを検出する。

    Bash コマンドが Codex/Gemini CLI を含む場合は `bash:codex` / `bash:gemini`、
    Task/Agent ツールは `task:<agent_type>`、Skill ツールは `skill:<name>` を返す。

    Args:
        data: PostToolUse hook の入力辞書。

    Returns:
        (route, command_excerpt, tool_name) のタプル。
        ルート検出不可の場合は route=None。
    """
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
            return "bash:codex", command[:200], tool_name
        if "gemini" in cmd_lower:
            return "bash:gemini", command[:200], tool_name
        return None, command[:200], tool_name

    if tool_lower in ("task", "agent"):
        subagent_type = "agent"
        if isinstance(tool_input, dict):
            subagent_type = str(
                tool_input.get("subagent_type")
                or tool_input.get("agent_type")
                or tool_input.get("agent")
                or "agent"
            )
        return f"task:{subagent_type}", "", tool_name

    if tool_lower == "skill":
        skill_name = "unknown"
        if isinstance(tool_input, dict):
            skill_name = str(tool_input.get("skill") or tool_input.get("skill_name") or "unknown")
        return f"skill:{skill_name}", "", tool_name

    return None, "", tool_name


def is_match(expected_route: str, actual_route: str, aliases: dict) -> bool:
    """予測ルートと実ルートが一致するか判定する。

    マッチ条件:
    - 完全一致
    - aliases[expected_route] に actual_route が含まれる

    `skill:*` も aliases 経由で許可する必要があり、ポリシー未定義の skill は
    不一致扱いとなる（過剰なマッチを避けるため）。

    Args:
        expected_route: 予測されたルート文字列。
        actual_route: 実際に使用されたルート文字列。
        aliases: エイリアス定義辞書 (`expected_route` → 許容 actual のリスト)。

    Returns:
        一致していれば True。
    """
    if not expected_route or not actual_route:
        return False
    if expected_route == actual_route:
        return True
    if actual_route in (aliases.get(expected_route) or []):
        return True
    return False


def _parse_actual_route(actual_route: str) -> dict:
    """actual_route 文字列を構造化する。

    Args:
        actual_route: `bash:codex` のような文字列。

    Returns:
        `{"tool": "bash", "detail": "codex"}` 形式の辞書。
    """
    if ":" in actual_route:
        parts = actual_route.split(":", 1)
        return {"tool": parts[0], "detail": parts[1]}
    return {"tool": actual_route, "detail": None}


def _get_expected_route(trace: dict, root: str) -> str:
    """trace state から expected_route を取得する。

    Args:
        trace: `load_trace_state()` の結果辞書。
        root: プロジェクトルート。

    Returns:
        予測ルート文字列。trace になければ legacy state (`expected-route.json`) を参照。
    """
    expected_route = trace.get("expected_route", "")
    if expected_route:
        return expected_route
    legacy_state = read_json_safe(os.path.join(root, ".claude", "state", "expected-route.json"))
    return str(legacy_state.get("expected_route") or "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


@safe_hook_execution
def main() -> None:
    """PostToolUse hook のエントリポイント。

    実ルートを検出し、予測ルートとの一致判定を行って route_decision イベントを記録する。
    """
    data = read_hook_input()

    root = resolve_project_root_from_hook_data(data)
    flags = load_package_config("audit", "audit-flags.json", root)
    features = flags.get("features") or {}
    route_audit_cfg = features.get("route_audit") or {}

    if not route_audit_cfg.get("enabled", True):
        return

    actual_route, command_excerpt, tool_name = detect_route(data)
    if not actual_route:
        return

    config = load_config(data)
    policy = load_package_config("audit", "delegation-policy.json", root)
    all_aliases = {**build_aliases(config), **(policy.get("aliases") or {})}

    session_id = str(data.get("session_id") or "")
    trace = load_trace_state(project_dir=root)
    tid = trace.get("tid", "")
    expected_route = _get_expected_route(trace, root)

    matched = is_match(expected_route, actual_route, all_aliases)
    helper_routes = policy.get("helper_routes") or []
    is_helper = actual_route in helper_routes

    emit_event(
        "route_decision",
        {
            "expected": expected_route,
            "actual": _parse_actual_route(actual_route),
            "matched": matched,
            "is_helper": is_helper,
            "reason": None,
        },
        session_id=session_id,
        tid=tid,
        project_dir=root,
    )


if __name__ == "__main__":
    main()
