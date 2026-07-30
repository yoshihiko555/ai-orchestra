#!/usr/bin/env python3
"""
PostToolUse hook: Suggest review after significant implementation.

Tracks file edits across the session and suggests code review
when 3+ files or 100+ lines have been modified.

State is persisted to .claude/state/post-implementation-review.json
(resolved via quality_gate_config.resolve_state_path), so separate worktrees
of the same repo naturally get isolated counters. Within one project_dir,
state is additionally scoped per git-common-dir (see
quality_gate_config.get_project_state_key) for backward-compatible schema
consistency with the other quality-gates hooks. A suggestion is re-armed
after REVIEW_SUGGESTION_TTL_SECONDS has elapsed since it was last shown,
instead of staying suppressed forever.
"""

import json
import os
import sys
import time
from pathlib import Path

_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

from hook_common import load_package_config  # noqa: E402
from quality_gate_config import (  # noqa: E402
    get_project_state_key,
    load_project_scoped_state,
    resolve_quality_gate_enabled,
    resolve_state_path,
    save_project_scoped_state,
    update_project_scoped_state,
)

# Session state filename for tracking modifications. The actual path is
# resolved per-project via quality_gate_config.resolve_state_path().
STATE_FILENAME = "post-implementation-review.json"

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
    state_file = Path(resolve_state_path(project_dir, STATE_FILENAME))
    return load_project_scoped_state(state_file, project_key, _DEFAULT_IMPL_REVIEW_STATE)


def save_state(project_dir: str, state: dict) -> None:
    """Save session state to file (scoped to the current project)."""
    project_key = get_project_state_key(project_dir)
    state_file = Path(resolve_state_path(project_dir, STATE_FILENAME))
    save_project_scoped_state(state_file, project_key, state)


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

        # EV-21: quality_gate.enabled=false のときは提案・状態記録を含む
        # 全動作を行わない（test-gate-checker.py と同じ no-op パターン）。
        project_dir = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
        config = load_package_config("audit", "audit-flags.json", project_dir)
        quality_gate = config.get("features", {}).get("quality_gate", {})
        if not resolve_quality_gate_enabled(quality_gate):
            sys.exit(0)

        # Calculate lines changed
        content = tool_input.get("content", "") or tool_input.get("new_string", "")
        lines_changed = count_lines(content)

        # Update state atomically: a single locked read-modify-write critical
        # section covers both the counter accumulation and the
        # should_suggest_review decision, so two near-simultaneous hook
        # invocations (e.g. concurrent Edit calls) cannot race each other.
        project_key = get_project_state_key(project_dir)
        suggestion: dict = {"triggered": False, "file_count": 0, "total_lines": 0}

        def _mutate(state: dict) -> dict:
            state["files"].append(file_path)
            state["total_lines"] += lines_changed

            if should_suggest_review(state):
                # Capture pre-reset counts for the message before clearing the window.
                suggestion["triggered"] = True
                suggestion["file_count"] = len(set(state["files"]))
                suggestion["total_lines"] = state["total_lines"]

                state["review_suggested"] = True
                state["suggested_at"] = time.time()
                state["files"] = []
                state["total_lines"] = 0

            return state

        state_file = Path(resolve_state_path(project_dir, STATE_FILENAME))
        update_project_scoped_state(state_file, project_key, _mutate, _DEFAULT_IMPL_REVIEW_STATE)

        if suggestion["triggered"]:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"[Review Suggestion] Significant changes detected:\n"
                        f"- {suggestion['file_count']} files modified\n"
                        f"- ~{suggestion['total_lines']} lines changed\n\n"
                        "Consider running code review:\n"
                        "- `/review code` for code quality\n"
                        "- `/review security` for security issues\n"
                        "- `/review` for comprehensive review"
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
