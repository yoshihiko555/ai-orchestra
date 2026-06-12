#!/usr/bin/env python3
"""AI の失敗イベントを検知する純粋ユーティリティ（I/O を持たない）。

PostToolUse hook の入力（tool_name / tool_input / tool_response）から
「失敗」を判定し、失敗の事実（種別・エラー分類・検知根拠）を返す。

設計方針:
- 副作用を持たない純粋関数のみ。ログ書き込みは呼び出し側の責務。
- exit_code 単独判定の弱点（パイプで終了コードがマスクされる）を、
  test/lint コマンドに限った出力パターン判定で補う。
- fail-logs パッケージが先行利用し、将来 quality-gates も共有する想定。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 失敗種別 / エラー分類の定数
# ---------------------------------------------------------------------------

FAILURE_TOOL_ERROR = "tool_error"
FAILURE_TEST = "test_failure"
FAILURE_LINT = "lint_failure"
FAILURE_CLI = "cli_failure"

# 検知根拠（detected_by）
BY_EXIT_CODE = "exit_code"
BY_OUTPUT_PATTERN = "output_pattern"
BY_TOOL_ERROR = "tool_error"

# コマンド種別（command_kind）
KIND_TEST = "test"
KIND_LINT = "lint"
KIND_CLI = "cli"
KIND_SHELL = "shell"
KIND_NONE = "none"

# timeout コマンドの慣用終了コード（GNU coreutils）
TIMEOUT_EXIT_CODE = 124

# ---------------------------------------------------------------------------
# コマンド分類パターン
# ---------------------------------------------------------------------------

# テスト実行コマンド（quality-gates の TEST_COMMAND_PATTERNS と互換）
TEST_COMMAND_PATTERNS = [
    re.compile(r"\bpytest\b"),
    re.compile(r"\bnpm\s+test\b"),
    re.compile(r"\bnpm\s+run\s+test\b"),
    re.compile(r"\bpnpm\s+test\b"),
    re.compile(r"\byarn\s+test\b"),
    re.compile(r"\buv\s+run\s+pytest\b"),
    re.compile(r"\bpoe\s+test\b"),
    re.compile(r"\bgo\s+test\b"),
    re.compile(r"\bcargo\s+test\b"),
    re.compile(r"\bmake\s+test\b"),
]

# Lint / 型チェックコマンド
LINT_COMMAND_PATTERNS = [
    re.compile(r"\bruff\s+check\b"),
    re.compile(r"\bruff\s+format\b"),
    re.compile(r"\bmypy\b"),
    re.compile(r"\beslint\b"),
    re.compile(r"\bbiome\s+(check|lint)\b"),
    re.compile(r"\bprettier\b"),
    re.compile(r"\bgolangci-lint\b"),
]

# 外部 CLI（深い推論・リサーチ委譲）
CLI_COMMAND_PATTERNS = [
    re.compile(r"\bcodex\s+exec\b"),
    re.compile(r"(?:^|[\s;|&])agy\s", re.IGNORECASE),
    re.compile(r"(?:^|[\s;|&])gemini\s", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# 出力からの失敗マーカー
# ---------------------------------------------------------------------------

# 「0 failed」「no errors」を誤検知しないよう、非ゼロ件数や明示マーカーに限定する
FAILURE_MARKERS = [
    re.compile(r"\b[1-9]\d*\s+failed\b", re.IGNORECASE),  # pytest "1 failed"
    re.compile(r"^FAILED\b", re.MULTILINE),  # pytest FAILED 行
    re.compile(r"\bAssertionError\b"),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"\b[1-9]\d*\s+error(?:s)?\b", re.IGNORECASE),  # "2 errors" / ruff,mypy
    re.compile(r"^E\s+\w*Error", re.MULTILINE),  # pytest エラー行
]


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------


def classify_command(command: str) -> str:
    """Bash コマンドの種別を分類する。

    Args:
        command: Bash コマンド文字列。

    Returns:
        ``test`` / ``lint`` / ``cli`` / ``shell`` のいずれか。
        空文字列の場合は ``none``。
    """
    if not command:
        return KIND_NONE
    if any(p.search(command) for p in CLI_COMMAND_PATTERNS):
        return KIND_CLI
    if any(p.search(command) for p in TEST_COMMAND_PATTERNS):
        return KIND_TEST
    if any(p.search(command) for p in LINT_COMMAND_PATTERNS):
        return KIND_LINT
    return KIND_SHELL


def has_failure_markers(output: str) -> bool:
    """出力に失敗を示すマーカーが含まれるか判定する。

    Args:
        output: コマンドの標準出力（stderr 併合を含む場合あり）。

    Returns:
        失敗マーカーが 1 つでも見つかれば True。
    """
    if not output:
        return False
    return any(p.search(output) for p in FAILURE_MARKERS)


def classify_error(output: str, exit_code: int | None) -> str:
    """エラー種別を推定する。

    Args:
        output: コマンド出力。
        exit_code: 終了コード（None の場合は出力のみで判定）。

    Returns:
        ``timeout`` / ``auth`` / ``not_found`` / ``rate_limit`` /
        ``syntax`` / ``assertion`` / ``unknown`` のいずれか。
    """
    if exit_code == TIMEOUT_EXIT_CODE:
        return "timeout"
    text = (output or "").lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if (
        "unauthorized" in text
        or "permission denied" in text
        or "auth failed" in text
        or "authentication failed" in text
        or "403" in text
    ):
        return "auth"
    if "not found" in text or "no such file" in text:
        return "not_found"
    if "rate limit" in text or "429" in text:
        return "rate_limit"
    if "syntaxerror" in text or "syntax error" in text:
        return "syntax"
    if "assertionerror" in text:
        return "assertion"
    return "unknown"


def analyze(
    tool_name: str,
    tool_input: dict | None,
    tool_response: dict | None,
) -> dict | None:
    """PostToolUse 入力から失敗を判定する。

    Args:
        tool_name: ツール名（"Bash" / "Edit" / "Write" / "Agent" 等）。
        tool_input: ツール入力辞書。
        tool_response: ツール応答辞書。

    Returns:
        失敗時は失敗事実の辞書、失敗でなければ None。
        辞書のキー: ``failure_type`` / ``error_type`` /
        ``detected_by`` / ``command_kind``。
    """
    tool_input = tool_input or {}
    tool_response = tool_response or {}

    if tool_name == "Bash":
        command = tool_input.get("command", "") or ""
        exit_code = tool_response.get("exit_code")
        output = tool_response.get("stdout", "") or tool_response.get("content", "") or ""
        return _detect_bash(command, exit_code, output)

    return _detect_tool_error(tool_response)


# ---------------------------------------------------------------------------
# 内部ヘルパー
# ---------------------------------------------------------------------------


def _failure_type_for_kind(kind: str) -> str:
    """コマンド種別を失敗種別にマッピングする。"""
    if kind == KIND_TEST:
        return FAILURE_TEST
    if kind == KIND_LINT:
        return FAILURE_LINT
    if kind == KIND_CLI:
        return FAILURE_CLI
    return FAILURE_TOOL_ERROR


def _result(failure_type: str, error_type: str, detected_by: str, command_kind: str) -> dict:
    """失敗事実の辞書を組み立てる。"""
    return {
        "failure_type": failure_type,
        "error_type": error_type,
        "detected_by": detected_by,
        "command_kind": command_kind,
    }


def _detect_bash(command: str, exit_code: int | None, output: str) -> dict | None:
    """Bash コマンドの失敗を 2 段判定する。

    1. exit_code が明示的に非ゼロ → 失敗（最も信頼できる根拠）
    2. exit_code が 0 / None でも test/lint コマンドで出力に失敗マーカー
       → 失敗（パイプによる終了コードのマスクや exit_code 欠落への対策）
    """
    kind = classify_command(command)

    # 1. 終了コードによる判定
    if exit_code is not None and exit_code != 0:
        return _result(
            _failure_type_for_kind(kind),
            classify_error(output, exit_code),
            BY_EXIT_CODE,
            kind,
        )

    # 2. 出力パターンによる判定（test/lint のみ・パイプマスク対策）
    if kind in (KIND_TEST, KIND_LINT) and has_failure_markers(output):
        return _result(
            _failure_type_for_kind(kind),
            classify_error(output, exit_code),
            BY_OUTPUT_PATTERN,
            kind,
        )

    return None


def _detect_tool_error(tool_response: dict) -> dict | None:
    """非 Bash ツールの明示的エラーを検知する。

    誤検知を避けるため、内容文字列の走査ではなく構造化された
    エラーフィールド（``error`` / ``is_error``）のみを根拠とする。
    """
    error_text = _extract_error_text(tool_response)
    if not error_text:
        return None
    return _result(
        FAILURE_TOOL_ERROR,
        classify_error(error_text, None),
        BY_TOOL_ERROR,
        KIND_NONE,
    )


def _extract_error_text(tool_response: dict) -> str:
    """ツール応答から構造化エラーテキストを取り出す。なければ空文字列。"""
    error = tool_response.get("error")
    if isinstance(error, str) and error.strip():
        return error
    if tool_response.get("is_error"):
        content = tool_response.get("content", "") or tool_response.get("stdout", "")
        if isinstance(content, str) and content.strip():
            return content
        return "tool reported is_error"
    return ""
