#!/usr/bin/env python3
"""
PostToolUse hook: Suggest Codex debugging after test failures.

Triggers after Bash tool calls containing test commands (pytest, npm test, etc.)
when the test run fails.

Also records test results to the shared test-gate state file so that
test-gate-checker.py can reset change counters after successful tests.
"""

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

# hook_common / event_logger を import するため core/hooks と audit/hooks を sys.path に追加
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
_repo_core_hooks = os.path.abspath(os.path.join(_hook_dir, "..", "..", "core", "hooks"))
_repo_audit_hooks = os.path.abspath(os.path.join(_hook_dir, "..", "..", "audit", "hooks"))

for _candidate in [
    os.path.join(_orchestra_dir, "packages", "core", "hooks") if _orchestra_dir else "",
    os.path.join(_orchestra_dir, "packages", "audit", "hooks") if _orchestra_dir else "",
    _repo_core_hooks,
    _repo_audit_hooks,
]:
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from event_logger import emit_event, load_trace_state, resolve_project_root_from_hook_data  # noqa: E402
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

# Shared state file with test-gate-checker.py
TEST_GATE_STATE_FILE = Path("/tmp/claude-test-gate-state.json")


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


def load_test_gate_state() -> dict:
    """Load the shared test-gate state from file."""
    try:
        if TEST_GATE_STATE_FILE.exists():
            with open(TEST_GATE_STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {
        "files_modified_since_test": [],
        "lines_modified_since_test": 0,
        "last_test_result": None,
        "warned": False,
    }


def save_test_gate_state(state: dict) -> None:
    """Save the shared test-gate state to file."""
    try:
        TEST_GATE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TEST_GATE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def record_test_result(command: str, passed: bool) -> None:
    """Record test result to the shared state file.

    On success: reset change counters and warned flag.
    On failure: keep counters (changes are not yet validated).
    """
    state = load_test_gate_state()
    state["last_test_result"] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "passed": passed,
        "command": command,
    }
    if passed:
        state["files_modified_since_test"] = []
        state["lines_modified_since_test"] = 0
        state["warned"] = False
    save_test_gate_state(state)


def load_quality_gate_config(project_dir: str) -> dict:
    """audit-flags.json から quality_gate 設定を読み込む。"""
    config = load_package_config("audit", "audit-flags.json", project_dir)
    features = config.get("features", {})
    return features.get("quality_gate", {}) if isinstance(features, dict) else {}


def emit_quality_gate_event(
    data: dict,
    *,
    command: str,
    exit_code: int,
    output: str,
    passed: bool,
) -> bool:
    """品質ゲート結果を audit イベントログに記録する。

    Returns:
        `block_on_failed_test` によりブロックすべき場合は True。
    """
    project_dir = resolve_project_root_from_hook_data(data)
    quality_gate = load_quality_gate_config(project_dir)
    if quality_gate.get("enabled", True) is False:
        return False

    trace = load_trace_state(project_dir=project_dir)
    blocking = bool(quality_gate.get("block_on_failed_test", False)) and not passed

    emit_event(
        "quality_gate",
        {
            "command": command[:200],
            "exit_code": exit_code,
            "passed": passed,
            "output_excerpt": output[:200] if output else "",
            "blocking": blocking,
        },
        session_id=str(data.get("session_id") or ""),
        tid=trace.get("tid", ""),
        project_dir=project_dir,
    )
    return blocking


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

        passed = not is_test_failure(exit_code, output)

        # Record test result to shared state (success resets counters)
        record_test_result(command, passed)
        blocking = emit_quality_gate_event(
            data,
            command=command,
            exit_code=exit_code,
            output=output,
            passed=passed,
        )

        if blocking:
            print(f"[quality-gates] quality gate blocked: test failed (exit_code={exit_code})", file=sys.stderr)
            sys.exit(2)

        # If tests passed, no further action needed
        if passed:
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
