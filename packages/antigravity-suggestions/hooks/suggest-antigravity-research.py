#!/usr/bin/env python3
"""
PreToolUse hook: Suggest Antigravity for research tasks.

Analyzes web search/fetch operations and suggests using Antigravity CLI (agy)
for comprehensive research with its larger context window.
"""

import json
import os
import re
import sys

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

from hook_common import (  # noqa: E402
    is_cli_enabled,
    load_cli_tools_config,
)
from suggestion_common import claim_suggestion, is_subagent_context  # noqa: E402

RESEARCH_INTENT = re.compile(
    r"(?<=\S)\s+(?:vs\.?|versus)\s+(?=\S)|"
    r"\b(?:which\s+is\s+better|trade[- ]?offs?|evaluat(?:e|ing|ion)|alternatives?)\b|"
    r"違い|どっち|選定|トレードオフ",
    re.IGNORECASE,
)
COMPARISON_INTENT = re.compile(r"\bcompar(?:e|es|ing|ison|isons)\b|比較", re.IGNORECASE)
COMPARISON_OPTIONS = re.compile(r"\b(?:and|or|between|options?|patterns?)\b", re.IGNORECASE)
VALUE_COMPARISON = re.compile(
    r"\bcompar(?:e|es|ing|ison|isons)[ \t]+(?:of[ \t]+)?"
    r"(?:(?:two|multiple|a|an|the)[ \t]+)?"
    r"(?:strings?|numbers?|integers?|arrays?|lists?|booleans?|values?|dicts?|dictionaries)\b|"
    r"(?:文字列|数値|配列|リスト|真偽値)(?:の)?比較",
    re.IGNORECASE,
)
MIGRATION_INTENT = re.compile(r"\b(?:migration|migrat(?:e|ing))\b|移行", re.IGNORECASE)
TROUBLESHOOTING_INTENT = re.compile(
    r"\b(?:errors?|exceptions?|traceback|stack\s+trace)\b|エラー|例外", re.IGNORECASE
)


def should_suggest_antigravity(query: str, url: str = "") -> tuple[bool, str]:
    """質問本文の調査意図だけを見る。URL 引数は呼び出し互換性のため残す。"""
    match = RESEARCH_INTENT.search(query)
    if not match:
        comparison_query = VALUE_COMPARISON.sub("", query)
        comparison = COMPARISON_INTENT.search(comparison_query)
        if comparison:
            term = comparison.group(0).casefold()
            if term in ("comparison", "comparisons", "比較") or COMPARISON_OPTIONS.search(
                comparison_query
            ):
                match = comparison
    # 明確な選定・比較と同居するエラー語は、単純なトラブル調査と区別する。
    if not match and not TROUBLESHOOTING_INTENT.search(query):
        match = MIGRATION_INTENT.search(query)
    if match:
        return True, f"比較・選定・移行の調査意図: {match.group(0).strip()}"
    return False, ""


def _build_antigravity_command(config: dict) -> str:
    """config から Antigravity コマンド文字列を構築する。

    agy は無効な model slug でも exit 0 でデフォルトにフォールバックするため、
    model_allowlist 未掲載の場合は警告を併記する。
    """
    antigravity = config.get("antigravity", {})
    model = antigravity.get("model", "")
    model_flag = f" --model {model}" if model else ""
    command = f"`agy -p '...'{model_flag} 2>/dev/null`"

    allowlist = antigravity.get("model_allowlist") or []
    if model and allowlist and model not in allowlist:
        command += (
            f"\n[WARN] model '{model}' is not in antigravity.model_allowlist. "
            "agy silently falls back to its default model for unknown slugs."
        )
    return command


def main():
    try:
        data = json.load(sys.stdin)
        if is_subagent_context(data):
            sys.exit(0)

        # Antigravity CLI が無効化されている場合は提案をスキップ
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "") or data.get("cwd", "")
        config = load_cli_tools_config(project_dir)
        if not is_cli_enabled("antigravity", config):
            sys.exit(0)

        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input", {})

        query = ""
        url = ""
        if tool_name == "WebSearch":
            query = tool_input.get("query", "")
        elif tool_name == "WebFetch":
            url = tool_input.get("url", "")
            query = tool_input.get("prompt", "")

        should_suggest, reason = should_suggest_antigravity(query, url)

        target = " ".join(query.casefold().split())
        if should_suggest and claim_suggestion(data, project_dir, "antigravity-research", target):
            agy_cmd = _build_antigravity_command(config)
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": (
                        f"[Antigravity Suggestion] {reason}. "
                        "任意の助言です。明示 routing・スキルの委譲指定を優先し、"
                        "相談済み・適用外なら追加相談せず続行できます。比較調査が有用な場合のみ:\n"
                        f"{agy_cmd}"
                    ),
                }
            }
            print(json.dumps(output))

        sys.exit(0)

    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
