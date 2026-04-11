#!/usr/bin/env python3
"""UserPromptSubmit hook: 期待ルートを予測し prompt イベントを記録する。"""

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
    generate_id,
    resolve_project_root_from_hook_data,
    save_trace_state,
)
from hook_common import (
    find_first_text,
    load_package_config,
    read_hook_input,
    safe_hook_execution,
)
from route_config import detect_agent, get_agent_tool, load_config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_EXCERPT_CHARS = 160

# 機密情報パターン（API キー・トークン・パスワード等）
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(
        r"\b[A-Za-z0-9_-]{0,20}(api[_-]?key|token|password|secret|credential)\b\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\b(AKIA|ASIA|A3T)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
]


def _mask_secrets(text: str) -> str:
    """テキストから既知の機密情報パターンをマスクする。

    Args:
        text: 検査対象のテキスト。

    Returns:
        マスク済みテキスト。該当しない場合は元の文字列を返す。
    """
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


# ---------------------------------------------------------------------------
# Route prediction
# ---------------------------------------------------------------------------


def select_expected_route(prompt: str, config: dict, policy: dict) -> tuple[str, str | None]:
    """config 駆動 + policy フォールバックで期待ルートを決定する。

    Args:
        prompt: ユーザープロンプト全文。
        config: cli-tools.yaml から読み込んだ agent-routing 設定。
        policy: delegation-policy.json から読み込んだルーティングポリシー。

    Returns:
        (expected_route, matched_rule_id) のタプル。ルール非マッチ時は
        matched_rule_id は None。
    """
    agent, _trigger = detect_agent(prompt)
    if agent:
        tool = get_agent_tool(agent, config)
        return tool, f"agent:{agent}"

    prompt_lower = prompt.lower()
    rules = sorted(
        policy.get("rules") or [],
        key=lambda x: x.get("priority", 0),
        reverse=True,
    )
    for rule in rules:
        keywords = rule.get("keywords_any") or []
        if any(str(k).lower() in prompt_lower for k in keywords):
            return (
                str(rule.get("expected_route", policy.get("default_route", "claude-direct"))),
                str(rule.get("id")),
            )

    return str(policy.get("default_route", "claude-direct")), None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


@safe_hook_execution
def main() -> None:
    """UserPromptSubmit hook のエントリポイント。

    ユーザープロンプトから期待ルートを予測し、トレース ID を生成して
    後続 hook に引き継ぐ。機密情報マスキング済みの excerpt を記録する。
    """
    data = read_hook_input()

    root = resolve_project_root_from_hook_data(data)
    flags = load_package_config("audit", "audit-flags.json", root)
    audit_cfg = (flags.get("features") or {}).get("route_audit") or {}
    if not audit_cfg.get("enabled", True):
        return

    prompt = find_first_text(data, {"prompt", "user_prompt", "message", "text"})
    if not prompt:
        return

    config = load_config(data)
    policy = load_package_config("audit", "delegation-policy.json", root)
    expected_route, matched_rule = select_expected_route(prompt, config, policy)

    session_id = str(data.get("session_id") or "")

    # マスキング → truncate の順序で処理する
    # (config の max_excerpt_chars を読み、デフォルトは 160)
    max_chars = int(audit_cfg.get("max_excerpt_chars", DEFAULT_MAX_EXCERPT_CHARS))
    excerpt = prompt.strip().replace("\n", " ")
    excerpt = _mask_secrets(excerpt)
    excerpt = excerpt[:max_chars]

    tid = generate_id()
    save_trace_state(
        tid,
        session_id=session_id,
        expected_route=expected_route,
        project_dir=root,
    )

    emit_event(
        "prompt",
        {
            "user_input_excerpt": excerpt,
            "expected_route": expected_route,
            "matched_rule": matched_rule,
        },
        session_id=session_id,
        tid=tid,
        project_dir=root,
    )


if __name__ == "__main__":
    main()
