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
    load_trace_state,
    resolve_project_root_from_hook_data,
    save_trace_state,
)
from hook_common import (
    find_first_text,
    is_harness_prompt,
    load_package_config,
    read_hook_input,
    safe_hook_execution,
)
from route_config import detect_agent, get_agent_tool, load_config
from secret_masking import mask_secrets as _mask_secrets

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_EXCERPT_CHARS = 160

# `/name` または `/plugin:name` の後に空白か本文末尾が続くもの（`/Users/...` 等のパスは除く）
SLASH_COMMAND_RE = re.compile(r"^/([A-Za-z0-9][\w.-]*(?::[\w.-]+)?)(?=\s|$)")

# ---------------------------------------------------------------------------
# Route prediction
# ---------------------------------------------------------------------------


def classify_prompt(prompt: str) -> tuple[str, str]:
    """prompt を入力種別とスラッシュコマンド名に分ける。

    Args:
        prompt: UserPromptSubmit の prompt 本文。

    Returns:
        (input_kind, slash_name) のタプル。input_kind は `harness`（タスク完了通知・
        ハンドバック等の非ユーザー入力）/ `slash` / `human` のいずれか。
        slash_name は `slash` のときだけ値を持ち、それ以外は空文字。
    """
    if is_harness_prompt(prompt):
        return "harness", ""
    match = SLASH_COMMAND_RE.match(prompt.lstrip())
    if match:
        return "slash", match.group(1)
    return "human", ""


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


def carry_over_route(previous: dict, session_id: str, policy: dict) -> tuple[str, str]:
    """非ユーザー入力のターンに、同じセッションの直前のターンの期待ルートとスキルを引き継ぐ。

    通知やハンドバックの本文はキーワード予測の対象にしない（本文の語で期待ルートが変わるため）。

    Args:
        previous: `load_trace_state()` の結果辞書。
        session_id: 現在のセッション識別子。
        policy: delegation-policy.json から読み込んだルーティングポリシー。

    Returns:
        (expected_route, skill) のタプル。引き継げない場合は既定ルートと空文字。
    """
    default_route = str(policy.get("default_route", "claude-direct"))
    if not session_id or previous.get("session_id") != session_id:
        return default_route, ""
    return str(previous.get("expected_route") or default_route), str(previous.get("skill") or "")


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
    session_id = str(data.get("session_id") or "")

    input_kind, slash_name = classify_prompt(prompt)
    if input_kind == "harness":
        previous = load_trace_state(project_dir=root)
        expected_route, skill = carry_over_route(previous, session_id, policy)
        matched_rule = None
    else:
        expected_route, matched_rule = select_expected_route(prompt, config, policy)
        skill = slash_name

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
        skill=skill,
        project_dir=root,
    )

    emit_event(
        "prompt",
        {
            "user_input_excerpt": excerpt,
            "input_kind": input_kind,
            "expected_route": expected_route,
            "matched_rule": matched_rule,
        },
        session_id=session_id,
        tid=tid,
        project_dir=root,
    )


if __name__ == "__main__":
    main()
