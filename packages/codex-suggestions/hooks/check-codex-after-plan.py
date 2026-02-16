#!/usr/bin/env python3
"""
PostToolUse hook: Suggest Codex review after Plan agent execution.

Triggers after Task tool calls with Plan agent to suggest
Codex review of the generated plan.
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

from hook_common import load_package_config  # noqa: E402


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


def _build_codex_command(data: dict) -> str:
    """cli-tools.yaml から Codex コマンド文字列を構築する。"""
    project_dir = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    config = load_package_config("agent-routing", "cli-tools.yaml", project_dir)
    codex = config.get("codex", {})
    model = codex.get("model", "gpt-5.3-codex")
    sandbox = codex.get("sandbox", {}).get("analysis", "read-only")
    flags = codex.get("flags", "--full-auto")
    return f'`codex exec --model {model} --sandbox {sandbox} {flags} "..." 2>/dev/null`'


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

        codex_cmd = _build_codex_command(data)
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    "[Codex Review Suggestion] Plan created. "
                    "Consider having Codex review the plan for:\n"
                    "- Architecture alignment\n"
                    "- Potential risks\n"
                    "- Missing considerations\n\n"
                    f"Use: {codex_cmd}"
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
