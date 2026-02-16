#!/usr/bin/env python3
"""
PostToolUse hook: Log Codex/Gemini CLI input/output to JSONL file.

Triggers after Bash tool calls containing 'codex' or 'gemini' commands.
Logs are stored in logs/cli-tools.jsonl

All agents (Claude Code, subagents, Codex, Gemini) can read this log.
"""

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

# codex exec コマンドの検知（timeout やenv変数プレフィックス付きも対応）
CODEX_EXEC_RE = re.compile(
    r"(?:^|&&|\|\||;|\|)\s*"
    r"(?:timeout\s+\d+\s+)?"
    r"(?:\w+=\S+\s+)*codex\s+exec\b",
    re.IGNORECASE,
)
# gemini -p コマンドの検知（-m などの前置フラグ、timeout/env プレフィックスも対応）
GEMINI_EXEC_RE = re.compile(
    r"(?:^|&&|\|\||;|\|)\s*"
    r"(?:timeout\s+\d+\s+)?"
    r"(?:\w+=\S+\s+)*gemini(?=\s|$)"
    r"(?:(?!&&|\|\||;|\|).)*\s+-p\b",
    re.IGNORECASE,
)


def get_log_path() -> Path:
    """Get log file path in project's .claude/logs/ directory."""
    # Try to find project root by looking for .claude/ directory
    cwd = Path.cwd()

    # Check current directory and parents for .claude/
    for parent in [cwd, *cwd.parents]:
        claude_dir = parent / ".claude"
        if claude_dir.is_dir():
            log_dir = claude_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            return log_dir / "cli-tools.jsonl"

    # Fallback: create .claude/logs/ in current directory
    log_dir = cwd / ".claude" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "cli-tools.jsonl"


def extract_codex_prompt(command: str) -> str | None:
    """Extract prompt from codex exec command."""
    # Pattern: codex exec ... "prompt" or codex exec ... 'prompt'
    patterns = [
        r'codex\s+exec\s+.*?--full-auto\s+"([^"]+)"',
        r"codex\s+exec\s+.*?--full-auto\s+'([^']+)'",
        r'codex\s+exec\s+.*?"([^"]+)"\s*2>/dev/null',
        r"codex\s+exec\s+.*?'([^']+)'\s*2>/dev/null",
    ]
    for pattern in patterns:
        match = re.search(pattern, command, re.DOTALL)
        if match:
            return match.group(1).strip()
    return None


def extract_gemini_prompt(command: str) -> str | None:
    """Extract prompt from gemini command."""
    # Pattern: gemini ... -p "prompt" or gemini ... -p 'prompt'
    patterns = [
        r'gemini(?=\s|$)(?:(?!&&|\|\||;|\|).)*?\s+-p\s+"([^"]+)"',
        r"gemini(?=\s|$)(?:(?!&&|\|\||;|\|).)*?\s+-p\s+'([^']+)'",
    ]
    for pattern in patterns:
        match = re.search(pattern, command, re.DOTALL)
        if match:
            return match.group(1).strip()
    return None


def extract_model(command: str, tool: str = "codex") -> str | None:
    """Extract model name from command.

    Codex uses --model flag, Gemini uses -m flag.
    """
    if tool == "gemini":
        match = re.search(r"(?:^|[\s;|&])gemini\s+.*?-m\s+(\S+)", command)
        return match.group(1) if match else None
    match = re.search(r"--model\s+(\S+)", command)
    return match.group(1) if match else None


def truncate_text(text: str, max_length: int = 2000) -> str:
    """Truncate text if too long."""
    if len(text) <= max_length:
        return text
    return text[:max_length] + f"... [truncated, {len(text)} total chars]"


def log_entry(entry: dict) -> None:
    """Append entry to JSONL log file."""
    log_file = get_log_path()
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> None:
    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        return

    # Only process Bash tool calls
    tool_name = hook_input.get("tool_name", "")
    if tool_name != "Bash":
        return

    # Get command and output
    tool_input = hook_input.get("tool_input", {})
    tool_response = hook_input.get("tool_response", {})

    command = tool_input.get("command", "")
    output = tool_response.get("stdout", "") or tool_response.get("content", "")

    # Check if this is a codex or gemini command (exact CLI invocation only)
    is_codex = bool(CODEX_EXEC_RE.search(command))
    is_gemini = bool(GEMINI_EXEC_RE.search(command)) and not is_codex

    if not (is_codex or is_gemini):
        return

    # Extract prompt based on tool type
    if is_codex:
        tool = "codex"
        prompt = extract_codex_prompt(command)
        model = extract_model(command) or "gpt-5.2-codex"
    else:
        tool = "gemini"
        prompt = extract_gemini_prompt(command)
        model = extract_model(command, tool="gemini") or "gemini-unknown"

    if not prompt:
        # Could not extract prompt, skip logging
        return

    # Determine success
    exit_code = tool_response.get("exit_code", 0)
    success = exit_code == 0 and bool(output)

    # Create log entry
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "tool": tool,
        "model": model,
        "prompt": truncate_text(prompt),
        "response": truncate_text(output) if output else "",
        "success": success,
        "exit_code": exit_code,
    }

    log_entry(entry)

    # 統一イベントログ ($AI_ORCHESTRA_DIR/packages/core/hooks/log_common.py)
    try:
        _orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
        if _orchestra_dir:
            _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
            if _core_hooks not in sys.path:
                sys.path.insert(0, _core_hooks)
            from log_common import append_event

            append_event(
                "cli_call",
                {
                    "tool": tool,
                    "model": model,
                    "prompt": truncate_text(prompt, 200),
                    "success": success,
                },
                session_id=hook_input.get("session_id", ""),
                hook_name="log-cli-tools",
            )
    except Exception:
        pass

    # Output notification (shown to user via hook output)
    print(
        json.dumps(
            {
                "result": "continue",
                "message": f"[LOG] {tool.capitalize()} call logged to .claude/logs/cli-tools.jsonl",
            }
        )
    )


if __name__ == "__main__":
    main()
