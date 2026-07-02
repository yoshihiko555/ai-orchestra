#!/usr/bin/env python3
"""
PreToolUse hook: Suggest Antigravity for research tasks.

Analyzes web search/fetch operations and suggests using Antigravity CLI (agy)
for comprehensive research with its larger context window.
"""

import json
import os
import sys

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

from hook_common import (  # noqa: E402
    is_cli_enabled,
    load_package_config,
    normalize_cli_tools_config,
)

# Keywords that suggest deep research would benefit from Antigravity
RESEARCH_INDICATORS = [
    "documentation",
    "best practice",
    "comparison",
    "library",
    "framework",
    "tutorial",
    "guide",
    "example",
    "pattern",
    "architecture",
    "migration",
    "upgrade",
    "breaking change",
    "api reference",
    "specification",
]

# Simple lookups that don't need Antigravity
# 注意: "version" は "versioning" 等にも部分一致してしまうため、より具体的な
# フレーズに限定する（例: "api versioning" のような強い研究シグナルを誤抑制しない）
SIMPLE_LOOKUP_PATTERNS = [
    "error message",
    "stack trace",
    "latest version",
    "what version",
    "release notes",
    "changelog",
]

# 複雑なリサーチとみなすクエリ長の閾値
COMPLEX_QUERY_LENGTH = 100


def should_suggest_antigravity(query: str, url: str = "") -> tuple[bool, str]:
    """Determine if Antigravity should be suggested for this research.

    RESEARCH_INDICATORS（強い研究シグナル）を SIMPLE_LOOKUP_PATTERNS より
    先に判定する。例えば "compare api versioning migration patterns" は
    "migration" 等の研究シグナルを含むため、単純な version 確認とは区別して
    提案対象にする。
    """
    query_lower = query.lower()
    url_lower = url.lower()
    combined = f"{query_lower} {url_lower}"

    for indicator in RESEARCH_INDICATORS:
        if indicator in combined:
            return True, f"Research involves '{indicator}'"

    for pattern in SIMPLE_LOOKUP_PATTERNS:
        if pattern in combined:
            return False, ""

    if len(query) > COMPLEX_QUERY_LENGTH:
        return True, "Complex research query detected"

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

        # Antigravity CLI が無効化されている場合は提案をスキップ
        project_dir = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
        config = normalize_cli_tools_config(
            load_package_config("agent-routing", "cli-tools.yaml", project_dir)
        )
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

        if should_suggest:
            agy_cmd = _build_antigravity_command(config)
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": (
                        f"[Antigravity Suggestion] {reason}. "
                        "For comprehensive research, consider Antigravity CLI "
                        "(large context + Google Search grounding):\n"
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
