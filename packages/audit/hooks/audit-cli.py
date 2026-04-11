#!/usr/bin/env python3
"""PostToolUse:Bash hook: Codex/Gemini CLI 呼び出しを検出し cli_call を記録する。"""

from __future__ import annotations

import os
import re
import sys

_hook_dir = os.path.dirname(os.path.abspath(__file__))
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)
    _audit_hooks = os.path.join(_orchestra_dir, "packages", "audit", "hooks")
    if _audit_hooks not in sys.path:
        sys.path.insert(0, _audit_hooks)
else:
    if _hook_dir not in sys.path:
        sys.path.insert(0, _hook_dir)

from event_logger import emit_event, load_trace_state
from hook_common import read_hook_input, safe_hook_execution

# ---------------------------------------------------------------------------
# CLI detection patterns (ported from log-cli-tools.py)
# ---------------------------------------------------------------------------

# 機密情報パターン（API キー・トークン・パスワード等）
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(
        r"\b[A-Za-z0-9_-]{0,20}(api[_-]?key|token|password|secret|credential)\b\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
]


def _mask_secrets(text: str) -> str:
    """テキストから既知の機密情報パターンをマスクする。"""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


CODEX_EXEC_RE = re.compile(
    r"(?:^|&&|\|\||;|\|)\s*"
    r"(?:timeout\s+\d+\s+)?"
    r"(?:\w+=\S+\s+)*codex\s+exec\b",
    re.IGNORECASE,
)

GEMINI_EXEC_RE = re.compile(
    r"(?:^|&&|\|\||;|\|)\s*"
    r"(?:timeout\s+\d+\s+)?"
    r"(?:\w+=\S+\s+)*gemini(?=\s|$)"
    r"(?:(?!&&|\|\||;|\|).)*\s+-p\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Prompt / model extraction
# ---------------------------------------------------------------------------


def extract_codex_prompt(command: str) -> str | None:
    """codex exec コマンドからプロンプトを抽出する。"""
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
    """gemini コマンドからプロンプトを抽出する。"""
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
    """コマンドからモデル名を抽出する。"""
    if tool == "gemini":
        match = re.search(r"(?:^|[\s;|&])gemini\s+.*?-m\s+(\S+)", command)
        return match.group(1) if match else None
    match = re.search(r"--model\s+(\S+)", command)
    return match.group(1) if match else None


def _classify_error(exit_code: int, output: str) -> str | None:
    """エラー種別を推定する。"""
    if exit_code == 0:
        return None
    output_lower = output.lower()
    if "timeout" in output_lower or "timed out" in output_lower:
        return "timeout"
    if "auth" in output_lower or "unauthorized" in output_lower or "403" in output_lower:
        return "auth"
    if "not found" in output_lower or "command not found" in output_lower:
        return "not_found"
    if "rate limit" in output_lower or "429" in output_lower:
        return "rate_limit"
    return "unknown"


def _resolve_project_root(data: dict) -> str:
    cwd = str(data.get("cwd") or "")
    if cwd and os.path.isdir(os.path.join(cwd, ".claude")):
        return cwd
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


@safe_hook_execution
def main() -> None:
    data = read_hook_input()

    if data.get("tool_name") != "Bash":
        return

    tool_input = data.get("tool_input", {})
    tool_response = data.get("tool_response", {})
    command = tool_input.get("command", "")
    output = tool_response.get("stdout", "") or tool_response.get("content", "")

    is_codex = bool(CODEX_EXEC_RE.search(command))
    is_gemini = bool(GEMINI_EXEC_RE.search(command)) and not is_codex

    if not (is_codex or is_gemini):
        return

    if is_codex:
        tool = "codex"
        prompt = extract_codex_prompt(command)
        model = extract_model(command) or ""
    else:
        tool = "gemini"
        prompt = extract_gemini_prompt(command)
        model = extract_model(command, tool="gemini") or ""

    if not prompt:
        return

    exit_code = tool_response.get("exit_code", 0)
    success = exit_code == 0 and bool(output)
    error_type = _classify_error(exit_code, output) if not success else None

    duration_ms = tool_response.get("duration_ms")

    session_id = str(data.get("session_id", ""))
    root = _resolve_project_root(data)
    trace = load_trace_state(project_dir=root)
    tid = trace.get("tid", "")

    emit_event(
        "cli_call",
        {
            "tool": tool,
            "model": model,
            "prompt": _mask_secrets(prompt),
            "response": _mask_secrets(output),
            "success": success,
            "exit_code": exit_code,
            "error_type": error_type,
            "duration_ms": duration_ms,
            "retry_count": 0,
        },
        session_id=session_id,
        tid=tid,
        project_dir=root,
    )


if __name__ == "__main__":
    main()
