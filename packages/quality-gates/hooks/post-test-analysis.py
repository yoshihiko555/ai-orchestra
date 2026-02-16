#!/usr/bin/env python3
"""
PostToolUse hook: Suggest Codex debugging after test failures.

Triggers after Bash tool calls containing test commands (pytest, npm test, etc.)
when the test run fails.
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

from hook_common import load_package_config  # noqa: E402

# Test command patterns
TEST_COMMAND_PATTERNS = [
    r"\bpytest\b",
    r"\bnpm\s+test\b",
    r"\bnpm\s+run\s+test\b",
    r"\bpnpm\s+test\b",
    r"\byarn\s+test\b",
    r"\buv\s+run\s+pytest\b",
    r"\bpoe\s+test\b",
    r"\bgo\s+test\b",
    r"\bcargo\s+test\b",
    r"\bmake\s+test\b",
]


def is_test_command(command: str) -> bool:
    """Check if the command is a test command."""
    command_lower = command.lower()
    return any(re.search(pattern, command_lower) for pattern in TEST_COMMAND_PATTERNS)


def is_test_failure(exit_code: int, output: str) -> bool:
    """Check if the test run failed."""
    if exit_code != 0:
        return True

    # Check output for failure indicators
    failure_indicators = [
        "FAILED",
        "FAIL:",
        "failed",
        "Error:",
        "error:",
        "AssertionError",
        "TypeError",
        "ValueError",
        "test failed",
        "tests failed",
    ]
    return any(indicator in output for indicator in failure_indicators)


def extract_failure_summary(output: str) -> str:
    """Extract a brief summary of the test failure."""
    lines = output.split("\n")

    # Look for lines containing failure information
    failure_lines = []
    for line in lines:
        if any(
            indicator in line for indicator in ["FAILED", "Error", "AssertionError", "TypeError"]
        ):
            failure_lines.append(line.strip())
            if len(failure_lines) >= 3:
                break

    if failure_lines:
        return "\n".join(failure_lines[:3])
    return "Test failure detected"


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

        # Only process Bash tool calls
        if tool_name != "Bash":
            sys.exit(0)

        tool_input = data.get("tool_input", {})
        tool_response = data.get("tool_response", {})

        command = tool_input.get("command", "")

        # Check if this is a test command
        if not is_test_command(command):
            sys.exit(0)

        exit_code = tool_response.get("exit_code", 0)
        output = tool_response.get("stdout", "") or tool_response.get("content", "")

        # Check if tests failed
        if not is_test_failure(exit_code, output):
            sys.exit(0)

        failure_summary = extract_failure_summary(output)
        codex_cmd = _build_codex_command(data)

        output_data = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    "[Codex Debug Suggestion] Test failure detected:\n"
                    f"```\n{failure_summary}\n```\n\n"
                    f"Consider Codex for root cause analysis:\n{codex_cmd}"
                ),
            }
        }
        print(json.dumps(output_data))
        sys.exit(0)

    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
