#!/usr/bin/env python3
"""SubagentStart / PreToolUse(Agent) hook: サブエージェントに routing と共有コンテキストを届ける。

処理フロー:
1. stdin から hook 入力 JSON を読み込む
2. 起動するエージェント名を決める
   - SubagentStart: `agent_type`（空なら general-purpose）
   - PreToolUse（`hook_event_name` が無い入力も含む）: tool_name が "Agent"（または後方互換の
     "Task"）のときだけ `tool_input.subagent_type`（省略時は general-purpose）
3. routing は context_store の利用可否と独立して解決する
4. SubagentStart: context_store が利用可能ならセッションエントリーと working-context を取得し、
   共有コンテキストと routing を結合して `additionalContext` で返す（サブエージェントの会話に
   hook の文脈として届く。prompt の外。`[Resolved Routing]` は常に最後に置く）
5. PreToolUse: routing の値だけを `additionalContext` で返す（オーケストレーターへの表示だけ）。
   共有コンテキストと `[Resolved Routing]` の見出しは含めず、`updatedInput` は返さない
   （auto mode の classifier が審査した後に入力を変えると拒否されるため。ADR-20261104-064）
6. 返す内容が空なら何もしない
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
# summary 以外の値（agent_id・task_name・working-context の各値）のトランケート文字数
_FIELD_TRUNCATE = 200
# working-context の追加フィールドの key のトランケート文字数
_KEY_TRUNCATE = 50
# modified_files の最大表示件数
_MAX_MODIFIED_FILES = 20

_SUBAGENT_START = "SubagentStart"
_PRE_TOOL_USE = "PreToolUse"
_AGENT_TOOL_NAMES = ("Agent", "Task")
# subagent_type / agent_type が空のときに起動されるエージェント
_DEFAULT_AGENT = "general-purpose"
# PreToolUse でオーケストレーターに見せるときの前置き（prompt へ書き写させないため）
_PREVIEW_NOTE = (
    "[Subagent Context Preview] Routing that the SubagentStart hook delivers to the subagent "
    "as hook context (shared context is not shown here). Do not copy it into the Agent prompt."
)


def _truncate(text: str, max_chars: int) -> str:
    """文字列を指定文字数にトランケートする。"""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _to_single_line(value: object) -> str:
    """任意の値を改行を含まない 1 行の文字列に畳む（`str.splitlines` が区切る改行文字をすべて畳む）。"""
    text = value if isinstance(value, str) else str(value)
    return " ".join(text.splitlines())


def _inline(value: object, max_chars: int = _FIELD_TRUNCATE) -> str:
    """共有コンテキストに展開する値を 1 行に畳み、上限の文字数で切る。"""
    return _truncate(_to_single_line(value), max_chars)


def build_entries_section(entries: list[dict[str, Any]]) -> str:
    """セッションエントリーから注入テキストのセクションを構築する。

    最新 _MAX_ENTRIES 件のエントリーを使用する。summary は _SUMMARY_TRUNCATE 文字にトランケートする。
    """
    if not entries:
        return ""

    # タイムスタンプでソートして最新 N 件を取得（型が混ざっても比較できるよう文字列に寄せる）
    sorted_entries = sorted(
        entries,
        key=lambda e: str(e.get("timestamp") or ""),
        reverse=True,
    )
    recent = sorted_entries[:_MAX_ENTRIES]
    # 古い順に並び直して表示する
    recent = list(reversed(recent))

    lines = ["## Previous Agent Results"]
    for entry in recent:
        agent_id = _inline(entry.get("agent_id") or "unknown")
        task_name = _inline(entry.get("task_name") or "")
        summary = _inline(entry.get("summary") or "", _SUMMARY_TRUNCATE)
        lines.append(f"- {agent_id} ({task_name}): {summary}")

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
        lines.append(f"- Modified files: {', '.join(_inline(file) for file in limited)}")

    current_phase = working_ctx.get("current_phase") or ""
    if current_phase:
        lines.append(f"- Current phase: {_inline(current_phase)}")

    recent_decisions = working_ctx.get("recent_decisions") or ""
    if recent_decisions:
        lines.append(f"- Recent decisions: {_inline(recent_decisions)}")

    # modified_files と既定フィールド以外の追加フィールドも出力する（key も畳む。偽の見出し行を作らせないため）
    known_keys = {"modified_files", "current_phase", "recent_decisions", "updated_at"}
    for key, value in working_ctx.items():
        if key in known_keys:
            continue
        if value:
            lines.append(f"- {_inline(key, _KEY_TRUNCATE)}: {_inline(value)}")

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
    header = [
        "[Resolved Routing]",
        f"Resolved by hook from {source}. "
        "Follow these values; do not re-read the config files to decide tool or sandbox. "
        "Call only the CLIs that have lines below.",
    ]
    return "\n".join(header + build_routing_value_lines(routing))


def build_routing_value_lines(routing: dict) -> list[str]:
    """解決済み routing の値の行（`- key: value`）を描画する。見出しは付けない。"""
    lines = [
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

    return lines


def normalize_agent_name(value: object) -> str:
    """エージェント名を正規化する。空・文字列以外は general-purpose とみなす。"""
    if not isinstance(value, str) or not value:
        return _DEFAULT_AGENT
    return value


def resolve_routing(agent: str, project_dir: str) -> tuple[dict, bool] | None:
    """導入済み project の既知エージェントの routing を解決する。

    Returns:
        (解決済み routing, cli-tools.local.yaml の有無)。注入の対象外なら None
    """
    if not _ROUTING_AVAILABLE:
        return None
    if not has_project_config("agent-routing", "cli-tools.yaml", project_dir):
        return None

    config = load_cli_tools_config(project_dir)
    agents = config.get("agents")
    if not isinstance(agents, dict) or agent not in agents:
        return None

    routing = resolve_agent_routing(agent, config)
    local_path = os.path.join(
        project_dir, ".claude", "config", "agent-routing", "cli-tools.local.yaml"
    )
    return routing, os.path.isfile(local_path)


def build_routing_injection(agent: str, project_dir: str) -> str:
    """導入済み project の既知エージェント向け routing 注入を構築する。"""
    resolved = resolve_routing(agent, project_dir)
    if resolved is None:
        return ""
    routing, has_local_config = resolved
    return build_resolved_routing_section(routing, has_local_config)


def build_routing_preview(agent: str, project_dir: str) -> str:
    """PreToolUse でオーケストレーターに見せる routing の値を構築する。

    共有コンテキスト（信頼できない過去のサブエージェント出力）は含めず、`[Resolved Routing]` の
    見出しも付けない。prompt に書き写されても routing のブロックとして扱われないようにするため。
    """
    resolved = resolve_routing(agent, project_dir)
    if resolved is None:
        return ""
    routing, _ = resolved
    return "\n".join([_PREVIEW_NOTE, *build_routing_value_lines(routing)])


def resolve_target_agent(data: dict) -> tuple[str, str] | None:
    """hook 入力から (出力する hookEventName, 起動するエージェント名) を決める。

    対象外の入力なら None を返す。`hook_event_name` が無い入力は、従来どおり PreToolUse として扱う。
    """
    event = data.get("hook_event_name") or _PRE_TOOL_USE
    if event == _SUBAGENT_START:
        return _SUBAGENT_START, normalize_agent_name(data.get("agent_type"))
    if event != _PRE_TOOL_USE:
        return None

    # Agent ツール以外は何もしない（後方互換のため "Task" も許容）
    if data.get("tool_name") not in _AGENT_TOOL_NAMES:
        return None
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    return _PRE_TOOL_USE, normalize_agent_name(tool_input.get("subagent_type"))


def resolve_project_dir(data: dict) -> str:
    """hook 入力からプロジェクトディレクトリを解決する。"""
    if _CONTEXT_STORE_AVAILABLE:
        return get_project_dir(data)
    return os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or ""


def build_combined_injection(agent: str, project_dir: str) -> str:
    """共有コンテキストと routing を結合する。`[Resolved Routing]` は常に最後に置く。"""
    injection = ""
    if _CONTEXT_STORE_AVAILABLE:
        entries = read_entries(project_dir)
        working_ctx = read_working_context(project_dir)
        injection = build_injection_text(entries, working_ctx)

    routing_text = build_routing_injection(agent, project_dir)
    if routing_text:
        injection = f"{injection}\n\n{routing_text}"
    return injection.strip()


@safe_hook_execution
def main() -> None:
    """SubagentStart / PreToolUse(Agent) hook のエントリポイント。"""
    try:
        raw = sys.stdin.read()
        data: dict = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(data, dict):
        return

    target = resolve_target_agent(data)
    if target is None:
        return
    event_name, agent = target
    project_dir = resolve_project_dir(data)

    if event_name == _SUBAGENT_START:
        # サブエージェントの会話に hook の文脈として届く
        additional_context = build_combined_injection(agent, project_dir)
    else:
        # オーケストレーターへの表示だけ。入力（prompt）は書き換えない
        additional_context = build_routing_preview(agent, project_dir)
    if not additional_context:
        return

    output = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": additional_context,
        }
    }
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
