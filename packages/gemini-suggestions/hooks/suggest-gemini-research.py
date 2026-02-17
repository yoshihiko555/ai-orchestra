#!/usr/bin/env python3
"""
PreToolUse hook: Suggest Gemini for research tasks.

Analyzes web search/fetch operations and suggests using Gemini CLI
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
    _routing_hooks = os.path.join(_orchestra_dir, "packages", "agent-routing", "hooks")
    if _routing_hooks not in sys.path:
        sys.path.insert(0, _routing_hooks)

from hook_common import load_package_config  # noqa: E402
from route_config import is_cli_enabled  # noqa: E402

# Keywords that suggest deep research would benefit from Gemini
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

# Simple lookups that don't need Gemini
SIMPLE_LOOKUP_PATTERNS = [
    "error message",
    "stack trace",
    "version",
    "release notes",
    "changelog",
]


def should_suggest_gemini(query: str, url: str = "") -> tuple[bool, str]:
    """Determine if Gemini should be suggested for this research."""
    query_lower = query.lower()
    url_lower = url.lower()
    combined = f"{query_lower} {url_lower}"

    for pattern in SIMPLE_LOOKUP_PATTERNS:
        if pattern in combined:
            return False, ""

    for indicator in RESEARCH_INDICATORS:
        if indicator in combined:
            return True, f"Research involves '{indicator}'"

    if len(query) > 100:
        return True, "Complex research query detected"

    return False, ""


def _build_gemini_command(config: dict) -> str:
    """config から Gemini コマンド文字列を構築する。"""
    gemini = config.get("gemini", {})
    model = gemini.get("model", "")
    model_flag = f"-m {model} " if model else ""
    return f"`gemini {model_flag}-p '...' 2>/dev/null`"


def main():
    try:
        data = json.load(sys.stdin)

        # Gemini CLI が無効化されている場合は提案をスキップ
        project_dir = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
        config = load_package_config("agent-routing", "cli-tools.yaml", project_dir)
        if not is_cli_enabled("gemini", config):
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

        should_suggest, reason = should_suggest_gemini(query, url)

        if should_suggest:
            gemini_cmd = _build_gemini_command(config)
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": (
                        f"[Gemini Suggestion] {reason}. "
                        "For comprehensive research, consider Gemini CLI (1M token context):\n"
                        f"{gemini_cmd}"
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
