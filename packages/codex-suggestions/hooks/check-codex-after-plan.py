#!/usr/bin/env python3
"""
PostToolUse hook: Suggest Codex review after Plan agent execution.

Triggers after Task tool calls with Plan agent to suggest
Codex review of the generated plan.
"""

import json
import sys


def is_plan_agent_task(tool_input: dict) -> bool:
    """Check if this was a Plan agent task."""
    subagent_type = tool_input.get("subagent_type", "").lower()
    prompt = tool_input.get("prompt", "").lower()

    # Check if subagent_type is Plan or plan-related
    if subagent_type in ("plan", "planner"):
        return True

    # Check prompt for planning keywords
    plan_keywords = [
        "計画",
        "プラン",
        "plan",
        "implementation plan",
        "設計計画",
        "実装計画",
    ]
    return any(keyword in prompt for keyword in plan_keywords)


def main():
    try:
        data = json.load(sys.stdin)
        tool_name = data.get("tool_name", "")

        # Only process Task tool calls
        if tool_name != "Task":
            sys.exit(0)

        tool_input = data.get("tool_input", {})
        tool_response = data.get("tool_response", {})

        # Check if this was a Plan agent task
        if not is_plan_agent_task(tool_input):
            sys.exit(0)

        # Check if the task completed successfully
        response_text = str(tool_response)
        if "error" in response_text.lower() or "failed" in response_text.lower():
            sys.exit(0)

        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    "[Codex Review Suggestion] Plan created. "
                    "Consider having Codex review the plan for:\n"
                    "- Architecture alignment\n"
                    "- Potential risks\n"
                    "- Missing considerations\n\n"
                    "Use: `codex exec --model gpt-5.2-codex --sandbox read-only "
                    '--full-auto "Review this implementation plan: ..."` 2>/dev/null'
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
