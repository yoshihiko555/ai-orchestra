#!/usr/bin/env python3
"""
PostToolUse hook: Suggest review after significant implementation.

Tracks file edits across the session and suggests code review
when 3+ files or 100+ lines have been modified.
"""

import json
import os
import sys
from pathlib import Path

# Session state file for tracking modifications
STATE_FILE = Path("/tmp/claude-impl-review-state.json")

# Thresholds for triggering review suggestion
FILE_THRESHOLD = 3
LINE_THRESHOLD = 100


def load_state() -> dict:
    """Load session state from file."""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {"files": [], "total_lines": 0, "review_suggested": False}


def save_state(state: dict) -> None:
    """Save session state to file."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def count_lines(content: str) -> int:
    """Count non-empty lines in content."""
    return len([line for line in content.split("\n") if line.strip()])


def should_suggest_review(state: dict) -> bool:
    """Check if review should be suggested."""
    if state["review_suggested"]:
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
        state = load_state()
        state["files"].append(file_path)
        state["total_lines"] += lines_changed

        if should_suggest_review(state):
            state["review_suggested"] = True
            save_state(state)

            file_count = len(set(state["files"]))
            total_lines = state["total_lines"]

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
            save_state(state)

        sys.exit(0)

    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
