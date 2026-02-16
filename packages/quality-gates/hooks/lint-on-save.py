#!/usr/bin/env python3
"""
PostToolUse hook: Run linting after Python file edits.

Triggers after Edit/Write tool calls on Python files
and runs ruff format/check and ty check.
"""

import json
import subprocess
import sys
from pathlib import Path


def is_python_file(file_path: str) -> bool:
    """Check if the file is a Python file."""
    return file_path.endswith(".py")


def run_lint_commands(file_path: str) -> list[dict]:
    """Run linting commands and return results."""
    results = []
    file_dir = str(Path(file_path).parent)

    # Commands to try (in order of preference)
    lint_commands = [
        # Try ruff format
        {
            "name": "ruff format",
            "commands": [
                ["uv", "run", "ruff", "format", file_path],
                ["ruff", "format", file_path],
            ],
        },
        # Try ruff check
        {
            "name": "ruff check",
            "commands": [
                ["uv", "run", "ruff", "check", "--fix", file_path],
                ["ruff", "check", "--fix", file_path],
            ],
        },
    ]

    for lint_config in lint_commands:
        for cmd in lint_config["commands"]:
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    cwd=file_dir,
                )
                # If command succeeded or found issues, record and break
                if result.returncode == 0 or result.stdout or result.stderr:
                    results.append(
                        {
                            "name": lint_config["name"],
                            "success": result.returncode == 0,
                            "output": (result.stdout or result.stderr or "").strip(),
                        }
                    )
                    break
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue

    return results


def main():
    try:
        data = json.load(sys.stdin)
        tool_name = data.get("tool_name", "")

        # Only process Edit/Write tool calls
        if tool_name not in ("Edit", "Write"):
            sys.exit(0)

        tool_input = data.get("tool_input", {})
        file_path = tool_input.get("file_path", "")

        # Only process Python files
        if not is_python_file(file_path):
            sys.exit(0)

        # Run lint commands
        results = run_lint_commands(file_path)

        if not results:
            # No linting tools available
            sys.exit(0)

        # Build output message
        messages = []
        has_issues = False

        for result in results:
            if result["success"]:
                if result["output"]:
                    messages.append(f"✓ {result['name']}: {result['output']}")
            else:
                has_issues = True
                messages.append(f"✗ {result['name']}: {result['output']}")

        if messages:
            status = "Issues found" if has_issues else "OK"
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": f"[Lint {status}] {file_path}\n" + "\n".join(messages),
                }
            }
            print(json.dumps(output))

        sys.exit(0)

    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
