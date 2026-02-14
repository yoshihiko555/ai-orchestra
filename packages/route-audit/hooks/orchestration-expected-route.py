#!/usr/bin/env python3
"""UserPromptSubmit hook: 期待ルートを決定して監査ログに保存する。"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
from typing import Any

_hook_dir = os.path.dirname(os.path.abspath(__file__))


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


def select_expected_route(prompt: str, policy: dict) -> tuple[str, str | None]:
    prompt_lower = prompt.lower()
    rules = sorted(policy.get("rules") or [], key=lambda x: x.get("priority", 0), reverse=True)

    for rule in rules:
        keywords = rule.get("keywords_any") or []
        if any(str(k).lower() in prompt_lower for k in keywords):
            return str(rule.get("expected_route", policy.get("default_route", "claude-direct"))), str(rule.get("id"))

    return str(policy.get("default_route", "claude-direct")), None


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
    flags = read_json(os.path.join(root, ".claude", "config", "route-audit", "orchestration-flags.json"))
    route_audit = ((flags.get("features") or {}).get("route_audit") or {})
    if not route_audit.get("enabled", True):
        sys.exit(0)

    prompt = find_first_text(data, {"prompt", "user_prompt", "message", "text"})
    if not prompt:
        sys.exit(0)

    policy = read_json(os.path.join(root, ".claude", "config", "route-audit", "delegation-policy.json"))
    expected_route, matched_rule = select_expected_route(prompt, policy)

    session_id = str(data.get("session_id") or "")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
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
    active = read_json(active_path)
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

    # 統一イベントログ
    try:
        _orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
        if _orchestra_dir:
            _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
            if _core_hooks not in sys.path:
                sys.path.insert(0, _core_hooks)
        from log_common import append_event
        append_event(
            "expected_route",
            {"prompt_id": prompt_id, "expected_route": expected_route, "matched_rule": matched_rule, "prompt_excerpt": excerpt},
            session_id=session_id,
            hook_name="expected-route",
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
