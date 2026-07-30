#!/usr/bin/env python3
"""
PostToolUse hook: Suggest running tests after significant code changes.

Tracks file edits across the session via a shared state file and suggests
test execution when the number of modified files or lines exceeds thresholds.

The shared state file (.claude/state/test-gate-checker.json, resolved via
quality_gate_config.resolve_state_path) is also written by
post-test-analysis.py, which resets counters on successful test runs.
The state file lives under the current project_dir (worktree), so separate
worktrees of the same repo naturally get isolated counters. Within one
project_dir, state is additionally scoped per git-common-dir (see
quality_gate_config.get_project_state_key) for backward-compatible schema
consistency with the other quality-gates hooks.
"""

import json
import os
import sys
from pathlib import Path

_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む。
# AI_ORCHESTRA_DIR 未設定の開発・検証環境向けに、リポジトリ内の
# core/hooks へのフォールバックも用意する（Issue #134 レビュー指摘:
# post-implementation-review.py と同じフォールバック欠落。
# quality_gate_config.py と同じフォールバック方式）。
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)
else:
    _fallback_core_hooks = Path(__file__).resolve().parents[2] / "core" / "hooks"
    if str(_fallback_core_hooks) not in sys.path:
        sys.path.insert(0, str(_fallback_core_hooks))

from hook_common import load_package_config  # noqa: E402
from log_common import find_project_root  # noqa: E402
from quality_gate_config import (  # noqa: E402
    DEFAULT_TEST_GATE_STATE,
    get_project_state_key,
    is_quality_gate_enabled,  # noqa: F401 (re-export; kept for external/test callers)
    load_project_scoped_state,
    resolve_quality_gate_enabled,
    resolve_state_path,
    save_project_scoped_state,
)

# Shared state filename with post-test-analysis.py. The actual path is
# resolved per-project via quality_gate_config.resolve_state_path().
STATE_FILENAME = "test-gate-checker.json"

# Code file extensions to track
CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java"}

# Default thresholds (overridden by audit-flags.json)
DEFAULT_FILE_THRESHOLD = 3
DEFAULT_LINE_THRESHOLD = 100


def load_test_gate_state(project_dir: str, config: dict | None = None) -> dict:
    """Load the shared test-gate state from file (scoped to the current project).

    `config` may be an already-loaded audit-flags.json dict (see main()) to
    avoid re-reading the config file within the same hook invocation.
    """
    project_key = get_project_state_key(project_dir)
    state_file = Path(resolve_state_path(project_dir, STATE_FILENAME, config=config))
    return load_project_scoped_state(state_file, project_key, DEFAULT_TEST_GATE_STATE)


def save_test_gate_state(project_dir: str, state: dict, config: dict | None = None) -> None:
    """Save the shared test-gate state to file (scoped to the current project).

    `config` may be an already-loaded audit-flags.json dict (see main()) to
    avoid re-reading the config file within the same hook invocation.
    """
    project_key = get_project_state_key(project_dir)
    state_file = Path(resolve_state_path(project_dir, STATE_FILENAME, config=config))
    save_project_scoped_state(state_file, project_key, state)


def is_code_file(file_path: str) -> bool:
    """Check if the file is a code file worth tracking."""
    return Path(file_path).suffix in CODE_EXTENSIONS


def count_lines(content: str) -> int:
    """Count non-empty lines in content."""
    return len([line for line in content.split("\n") if line.strip()])


def load_thresholds(project_dir: str) -> tuple[int, int]:
    """Load threshold values from audit-flags.json."""
    config = load_package_config("audit", "audit-flags.json", project_dir)
    quality_gate = config.get("features", {}).get("quality_gate", {})
    file_threshold = quality_gate.get("test_file_threshold", DEFAULT_FILE_THRESHOLD)
    line_threshold = quality_gate.get("test_line_threshold", DEFAULT_LINE_THRESHOLD)
    return file_threshold, line_threshold


def build_warning_message(file_count: int, line_count: int, has_test_history: bool) -> str:
    """Build the test gate warning message."""
    test_status = (
        "No tests have been run since last changes"
        if has_test_history
        else "No tests have been run in this session"
    )
    return (
        f"[Test Gate] Large changes without test execution:\n"
        f"- {file_count} files modified\n"
        f"- ~{line_count} lines changed\n"
        f"- {test_status}\n\n"
        "Consider running tests:\n"
        '- `Task(subagent_type="tester", prompt="Run tests for recent changes")`\n'
        "- or directly: `pytest` / `npm test` etc."
    )


def main() -> None:
    try:
        data = json.load(sys.stdin)
        tool_name = data.get("tool_name", "")

        # Only process Edit/Write tool calls
        if tool_name not in ("Edit", "Write"):
            sys.exit(0)

        tool_input = data.get("tool_input", {})
        file_path = tool_input.get("file_path", "")

        # Skip non-code files
        if not is_code_file(file_path):
            sys.exit(0)

        # Issue #134 レビュー指摘: subdirectory cwd でも project root の
        # .claude/config と共有 state を一貫して参照する。
        raw_project_dir = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
        project_dir = find_project_root(raw_project_dir) if raw_project_dir else find_project_root()

        # Read audit-flags.json once and reuse it for the enabled check, the
        # threshold lookup, and resolve_state_path()'s paths.state_dir lookup
        # below (avoids re-reading the same config file up to 4x per
        # Edit/Write event; code review: architecture-reviewer, Issue #154).
        config = load_package_config("audit", "audit-flags.json", project_dir)
        quality_gate = config.get("features", {}).get("quality_gate", {})
        if not resolve_quality_gate_enabled(quality_gate):
            sys.exit(0)

        # Calculate lines changed
        content = tool_input.get("content", "") or tool_input.get("new_string", "")
        lines_changed = count_lines(content)

        # Update state
        state = load_test_gate_state(project_dir, config=config)
        modified_files = state.get("files_modified_since_test", [])
        if file_path not in modified_files:
            modified_files.append(file_path)
        state["files_modified_since_test"] = modified_files
        state["lines_modified_since_test"] = (
            state.get("lines_modified_since_test", 0) + lines_changed
        )

        # Check thresholds (reuses the config already read above)
        file_threshold = quality_gate.get("test_file_threshold", DEFAULT_FILE_THRESHOLD)
        line_threshold = quality_gate.get("test_line_threshold", DEFAULT_LINE_THRESHOLD)
        file_count = len(modified_files)
        line_count = state["lines_modified_since_test"]
        already_warned = state.get("warned", False)

        should_warn = not already_warned and (
            file_count >= file_threshold or line_count >= line_threshold
        )

        if should_warn:
            state["warned"] = True
            save_test_gate_state(project_dir, state, config=config)

            has_test_history = state.get("last_test_result") is not None
            message = build_warning_message(file_count, line_count, has_test_history)

            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": message,
                }
            }
            print(json.dumps(output))
        else:
            save_test_gate_state(project_dir, state, config=config)

        sys.exit(0)

    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
