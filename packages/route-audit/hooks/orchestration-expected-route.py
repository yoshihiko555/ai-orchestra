#!/usr/bin/env python3
"""UserPromptSubmit hook: 期待ルートを決定して監査ログに保存する。"""

from __future__ import annotations

import datetime
import hashlib
import os
import sys

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

from hook_common import (  # noqa: E402
    append_jsonl,
    ensure_package_path,
    find_first_text,
    load_package_config,
    read_hook_input,
    read_json_safe,
    safe_hook_execution,
    try_append_event,
    write_json,
)

ensure_package_path("agent-routing")
from route_config import detect_agent, get_agent_tool, load_config  # noqa: E402

_hook_dir = os.path.dirname(os.path.abspath(__file__))


def select_expected_route(prompt: str, config: dict, policy: dict) -> tuple[str, str | None]:
    """config 駆動 + policy フォールバックで期待ルートを決定。"""
    # 1. Config 駆動: エージェント検出 → ツール取得
    agent, trigger = detect_agent(prompt)
    if agent:
        tool = get_agent_tool(agent, config)
        return tool, f"agent:{agent}"

    # 2. フォールバック: delegation-policy.json の keyword rules
    prompt_lower = prompt.lower()
    rules = sorted(policy.get("rules") or [], key=lambda x: x.get("priority", 0), reverse=True)

    for rule in rules:
        keywords = rule.get("keywords_any") or []
        if any(str(k).lower() in prompt_lower for k in keywords):
            return str(
                rule.get("expected_route", policy.get("default_route", "claude-direct"))
            ), str(rule.get("id"))

    return str(policy.get("default_route", "claude-direct")), None


def project_root(data: dict) -> str:
    return (
        data.get("cwd")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.path.abspath(os.path.join(_hook_dir, "..", "..", ".."))
    )


def main() -> None:
    data = read_hook_input()

    root = project_root(data)
    flags = load_package_config("route-audit", "orchestration-flags.json", root)
    route_audit = (flags.get("features") or {}).get("route_audit") or {}
    if not route_audit.get("enabled", True):
        sys.exit(0)

    prompt = find_first_text(data, {"prompt", "user_prompt", "message", "text"})
    if not prompt:
        sys.exit(0)

    config = load_config(data)
    policy = load_package_config("route-audit", "delegation-policy.json", root)
    expected_route, matched_rule = select_expected_route(prompt, config, policy)

    session_id = str(data.get("session_id") or "")
    now = datetime.datetime.now(datetime.UTC).isoformat()
    excerpt = prompt.strip().replace("\n", " ")[: int(route_audit.get("max_excerpt_chars", 160))]
    seed = f"{session_id}:{now}:{excerpt}"
    prompt_id = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]

    state_dir = os.path.join(root, ".claude", "state")
    logs_dir = os.path.join(root, ".claude", "logs", "orchestration")
    os.makedirs(state_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)

    payload = {
        "prompt_id": prompt_id,
        "session_id": session_id,
        "timestamp": now,
        "prompt_excerpt": excerpt,
        "expected_route": expected_route,
        "matched_rule": matched_rule,
    }

    write_json(os.path.join(state_dir, "expected-route.json"), payload)

    active_path = os.path.join(state_dir, "active.json")
    active = read_json_safe(active_path)
    active["expected_route"] = expected_route
    active["last_prompt_excerpt"] = excerpt
    active["updated_at"] = now
    write_json(active_path, active)

    append_jsonl(os.path.join(logs_dir, "expected-routes.jsonl"), payload)
    append_jsonl(
        os.path.join(logs_dir, "agent-trace.jsonl"),
        {
            "event": "expected_route_selected",
            "timestamp": now,
            "prompt_id": prompt_id,
            "expected_route": expected_route,
            "matched_rule": matched_rule,
        },
    )

    try_append_event(
        "expected_route",
        {
            "prompt_id": prompt_id,
            "expected_route": expected_route,
            "matched_rule": matched_rule,
            "prompt_excerpt": excerpt,
        },
        session_id=session_id,
        hook_name="expected-route",
        project_dir=root,
    )

    sys.exit(0)


if __name__ == "__main__":
    safe_hook_execution(main)()
