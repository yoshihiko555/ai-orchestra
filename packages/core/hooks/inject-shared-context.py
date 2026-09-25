#!/usr/bin/env python3
"""PreToolUse(Task) hook: 起動前の prompt に routing と共有コンテキストを注入する。

処理フロー:
1. stdin から PreToolUse JSON を読み込む
2. tool_name が "Agent"（または後方互換の "Task"）でなければ何もしない
3. routing は context_store の利用可否と独立して解決する
4. context_store が利用可能ならセッションエントリーと working-context を取得する
5. routing と共有コンテキストがどちらも空なら何もしない
6. 両セクションを結合し、最大 1 つの updatedInput で prompt の末尾に追加する
7. 変更後の tool_input を含む JSON を stdout に出力する
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# sys.path に packages/core/hooks を追加
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOK_DIR not in sys.path:
    sys.path.insert(0, _HOOK_DIR)

try:
    from hook_common import safe_hook_execution
except ImportError:
    import functools
    from collections.abc import Callable

    def safe_hook_execution(func: Callable[[], None]) -> Callable[[], None]:  # type: ignore[misc]
        """フォールバック: 例外時は stderr にログ出力して exit(0) する。"""

        @functools.wraps(func)
        def wrapper() -> None:
            try:
                func()
            except Exception as e:
                print(f"Hook error: {e}", file=sys.stderr)
                sys.exit(0)

        return wrapper


try:
    from hook_common import (
        has_project_config,
        load_cli_tools_config,
        resolve_agent_routing,
    )

    _ROUTING_AVAILABLE = True
except ImportError:
    _ROUTING_AVAILABLE = False


try:
    from context_store import get_project_dir, read_entries, read_working_context

    _CONTEXT_STORE_AVAILABLE = True
except ImportError:
    _CONTEXT_STORE_AVAILABLE = False

# 注入するエントリーの最大件数（最新 N 件）
_MAX_ENTRIES = 5
# 各エントリーの summary トランケート文字数
_SUMMARY_TRUNCATE = 200
# modified_files の最大表示件数
_MAX_MODIFIED_FILES = 20


def _truncate(text: str, max_chars: int) -> str:
    """文字列を指定文字数にトランケートする。"""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _to_single_line(value: object) -> str:
    """任意の値を改行を含まない 1 行の文字列に畳む。"""
    text = value if isinstance(value, str) else str(value)
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def build_entries_section(entries: list[dict[str, Any]]) -> str:
    """セッションエントリーから注入テキストのセクションを構築する。

    最新 _MAX_ENTRIES 件のエントリーを使用する。summary は _SUMMARY_TRUNCATE 文字にトランケートする。
    """
    if not entries:
        return ""

    # タイムスタンプでソートして最新 N 件を取得
    sorted_entries = sorted(
        entries,
        key=lambda e: e.get("timestamp") or "",
        reverse=True,
    )
    recent = sorted_entries[:_MAX_ENTRIES]
    # 古い順に並び直して表示する
    recent = list(reversed(recent))

    lines = ["## Previous Agent Results"]
    for entry in recent:
        agent_id = _to_single_line(entry.get("agent_id") or "unknown")
        task_name = _to_single_line(entry.get("task_name") or "")
        summary = _to_single_line(entry.get("summary") or "")
        truncated = _truncate(summary, _SUMMARY_TRUNCATE)
        lines.append(f"- {agent_id} ({task_name}): {truncated}")

    return "\n".join(lines)


def build_working_context_section(working_ctx: dict[str, Any]) -> str:
    """working-context から注入テキストのセクションを構築する。

    modified_files は最新 _MAX_MODIFIED_FILES 件に制限する。
    """
    if not working_ctx:
        return ""

    lines = ["## Working Context"]

    modified_files: list[str] = working_ctx.get("modified_files") or []
    if isinstance(modified_files, list) and modified_files:
        limited = modified_files[-_MAX_MODIFIED_FILES:]
        lines.append(f"- Modified files: {', '.join(_to_single_line(file) for file in limited)}")

    current_phase = working_ctx.get("current_phase") or ""
    if current_phase:
        lines.append(f"- Current phase: {_to_single_line(current_phase)}")

    recent_decisions = working_ctx.get("recent_decisions") or ""
    if recent_decisions:
        lines.append(f"- Recent decisions: {_to_single_line(recent_decisions)}")

    # modified_files と既定フィールド以外の追加フィールドも出力する
    known_keys = {"modified_files", "current_phase", "recent_decisions", "updated_at"}
    for key, value in working_ctx.items():
        if key in known_keys:
            continue
        if value:
            lines.append(f"- {key}: {_to_single_line(value)}")

    # セクション本文が "## Working Context" のみなら空扱いにする
    if len(lines) == 1:
        return ""

    return "\n".join(lines)


def build_injection_text(
    entries: list[dict[str, Any]],
    working_ctx: dict[str, Any],
) -> str:
    """注入テキスト全体を構築する。

    エントリーと working-context の両方が空なら空文字を返す。
    """
    sections: list[str] = []

    entries_section = build_entries_section(entries)
    if entries_section:
        sections.append(entries_section)

    ctx_section = build_working_context_section(working_ctx)
    if ctx_section:
        sections.append(ctx_section)

    if not sections:
        return ""

    body = "\n\n".join(sections)
    return f"\n\n[Shared Context]\n{body}"


def build_resolved_routing_section(routing: dict, has_local_config: bool = False) -> str:
    """解決済み routing をサブエージェント向けの固定形式で描画する。"""
    source = (
        "cli-tools.yaml + cli-tools.local.yaml (merged)" if has_local_config else "cli-tools.yaml"
    )
    lines = [
        "[Resolved Routing]",
        f"Resolved by hook from {source}. "
        "Follow these values; do not re-read the config files to decide tool or sandbox. "
        "Call only the CLIs that have lines below.",
        f"- agent: {routing['agent']}",
        f"- tool: {routing['tool']}",
    ]

    if "codex" in routing:
        codex = routing["codex"]
        lines.append(f"- codex.model: {codex['model']}")
        if "sandbox" in codex:
            lines.append(f"- codex.sandbox: {codex['sandbox']}")
        else:
            lines.append(f"- codex.sandbox.analysis: {codex['sandbox_analysis']}")
            lines.append(f"- codex.sandbox.implementation: {codex['sandbox_implementation']}")
        lines.append(f"- codex.flags: {codex['flags'] or '(none)'}")
        requires_sandbox_disable = "true" if codex["requires_sandbox_disable"] else "false"
        lines.append(f"- codex.requires_sandbox_disable: {requires_sandbox_disable}")

    if "antigravity" in routing:
        antigravity = routing["antigravity"]
        lines.append(f"- antigravity.model: {antigravity['model'] or '(CLI default)'}")
        lines.append(f"- antigravity.flags: {antigravity['flags'] or '(none)'}")
        allowlist_warning = antigravity.get("allowlist_warning")
        if isinstance(allowlist_warning, str) and allowlist_warning:
            lines.append(f"- WARN: {allowlist_warning}")

    for note in routing.get("notes", []):
        if isinstance(note, str):
            lines.append(f"- note: {note}")

    return "\n".join(lines)


def build_routing_injection(tool_input: dict, project_dir: str) -> str:
    """導入済み project の既知エージェント向け routing 注入を構築する。"""
    agent = tool_input.get("subagent_type")
    if not isinstance(agent, str) or not agent:
        agent = "general-purpose"

    if not _ROUTING_AVAILABLE:
        return ""
    if not has_project_config("agent-routing", "cli-tools.yaml", project_dir):
        return ""

    config = load_cli_tools_config(project_dir)
    agents = config.get("agents")
    if not isinstance(agents, dict) or agent not in agents:
        return ""

    routing = resolve_agent_routing(agent, config)
    local_path = os.path.join(
        project_dir, ".claude", "config", "agent-routing", "cli-tools.local.yaml"
    )
    return build_resolved_routing_section(routing, os.path.isfile(local_path))


@safe_hook_execution
def main() -> None:
    """PreToolUse(Task) hook のエントリポイント。"""
    try:
        raw = sys.stdin.read()
        data: dict = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(data, dict):
        return

    # Agent ツール以外は何もしない（後方互換のため "Task" も許容）
    tool_name = data.get("tool_name") or ""
    if tool_name not in ("Agent", "Task"):
        return

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return

    if _CONTEXT_STORE_AVAILABLE:
        project_dir = get_project_dir(data)
    else:
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or ""

    injection = ""
    if _CONTEXT_STORE_AVAILABLE:
        entries = read_entries(project_dir)
        working_ctx = read_working_context(project_dir)
        injection = build_injection_text(entries, working_ctx)

    routing_text = build_routing_injection(tool_input, project_dir)
    if not injection and not routing_text:
        return

    routing_part = f"\n\n{routing_text}" if routing_text else ""
    combined = injection + routing_part

    # additionalContext: オーケストレーターに表示される
    # updatedInput: サブエージェントの prompt に直接注入される
    original_prompt = tool_input.get("prompt") or ""
    new_tool_input = {**tool_input, "prompt": original_prompt + combined}
    additional_context = combined.strip()

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": additional_context,
            "updatedInput": new_tool_input,
        }
    }
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
