#!/usr/bin/env python3
"""
PostToolUse hook: Suggest review after significant implementation.

Tracks file edits across the session and suggests code review
when 3+ files or 100+ lines have been modified.

The state is scoped per project (see quality_gate_config.get_project_state_key)
so concurrent worktrees/sessions on different projects do not contaminate
each other's counters. A suggestion is re-armed after REVIEW_SUGGESTION_TTL_SECONDS
has elapsed since it was last shown, instead of staying suppressed forever.
"""

import json
import os
import sys
import time
from pathlib import Path

_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

from quality_gate_config import (  # noqa: E402
    get_project_state_key,
    load_project_scoped_state,
    save_project_scoped_state,
)

# Session state file for tracking modifications
STATE_FILE = Path("/tmp/claude-impl-review-state.json")

# Thresholds for triggering review suggestion
FILE_THRESHOLD = 3
LINE_THRESHOLD = 100

# How long a review suggestion stays "already suggested" before it can fire again.
REVIEW_SUGGESTION_TTL_SECONDS = 24 * 60 * 60  # 24 hours

_DEFAULT_IMPL_REVIEW_STATE: dict = {
    "files": [],
    "total_lines": 0,
    "review_suggested": False,
    "suggested_at": None,
}


def load_state(project_dir: str) -> dict:
    """Load session state from file (scoped to the current project)."""
    project_key = get_project_state_key(project_dir)
    return load_project_scoped_state(STATE_FILE, project_key, _DEFAULT_IMPL_REVIEW_STATE)


def save_state(project_dir: str, state: dict) -> None:
    """Save session state to file (scoped to the current project)."""
    project_key = get_project_state_key(project_dir)
    save_project_scoped_state(STATE_FILE, project_key, state)


def count_lines(content: str) -> int:
    """Count non-empty lines in content."""
    return len([line for line in content.split("\n") if line.strip()])


def is_suggestion_stale(state: dict) -> bool:
    """Return True when the last suggestion is older than the TTL (safe to re-suggest)."""
    suggested_at = state.get("suggested_at")
    if suggested_at is None:
        return False
    return (time.time() - suggested_at) >= REVIEW_SUGGESTION_TTL_SECONDS


def should_suggest_review(state: dict) -> bool:
    """Check if review should be suggested."""
    if state["review_suggested"] and not is_suggestion_stale(state):
        return False

    file_count = len(set(state["files"]))
    total_lines = state["total_lines"]

    return file_count >= FILE_THRESHOLD or total_lines >= LINE_THRESHOLD


def main():
    try:
        data = json.load(sys.stdin)
        tool_name = data.get("tool_name", "")

        # Only process Edit/Write tool calls
        if tool_name not in ("Edit", "Write"):
            sys.exit(0)

        tool_input = data.get("tool_input", {})
        file_path = tool_input.get("file_path", "")

        # Skip non-code files
        code_extensions = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java"}
        if not any(file_path.endswith(ext) for ext in code_extensions):
            sys.exit(0)

        # Calculate lines changed
        content = tool_input.get("content", "") or tool_input.get("new_string", "")
        lines_changed = count_lines(content)

        # Update state
        project_dir = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
        state = load_state(project_dir)
        state["files"].append(file_path)
        state["total_lines"] += lines_changed

        if should_suggest_review(state):
            # Capture pre-reset counts for the message before clearing the window.
            file_count = len(set(state["files"]))
            total_lines = state["total_lines"]

            state["review_suggested"] = True
            state["suggested_at"] = time.time()
            state["files"] = []
            state["total_lines"] = 0
            save_state(project_dir, state)

            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"[Review Suggestion] Significant changes detected:\n"
                        f"- {file_count} files modified\n"
                        f"- ~{total_lines} lines changed\n\n"
                        "Consider running code review:\n"
                        "- `/review code` for code quality\n"
                        "- `/review security` for security issues\n"
                        "- `/review` for comprehensive review"
                    ),
                }
            }
            print(json.dumps(output))
        else:
            save_state(project_dir, state)

        sys.exit(0)

    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
