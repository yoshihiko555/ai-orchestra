#!/usr/bin/env python3
"""
PreToolUse hook: Suggest Gemini for research tasks.

Analyzes web search/fetch operations and suggests using Gemini CLI
for comprehensive research with its larger context window.
"""

import json
import sys

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


def main():
    try:
        data = json.load(sys.stdin)
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
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": (
                        f"[Gemini Suggestion] {reason}. "
                        "For comprehensive research, consider Gemini CLI (1M token context):\n"
                        "`gemini -p '...' 2>/dev/null`"
                    )
                }
            }
            print(json.dumps(output))

        sys.exit(0)

    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
