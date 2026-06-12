"""failure_detector の純粋ロジックを検証する。"""

from __future__ import annotations

from tests.module_loader import load_module

fd = load_module("failure_detector", "packages/core/hooks/failure_detector.py")


# ---------------------------------------------------------------------------
# classify_command
# ---------------------------------------------------------------------------


def test_classify_command_test() -> None:
    assert fd.classify_command("pytest -q tests/") == fd.KIND_TEST
    assert fd.classify_command("uv run pytest") == fd.KIND_TEST


def test_classify_command_lint() -> None:
    assert fd.classify_command("ruff check .") == fd.KIND_LINT
    assert fd.classify_command("mypy src") == fd.KIND_LINT


def test_classify_command_cli() -> None:
    assert fd.classify_command('codex exec --model x "q" < /dev/null') == fd.KIND_CLI
    assert fd.classify_command("agy -p 'research'") == fd.KIND_CLI


def test_classify_command_cli_takes_priority_over_test() -> None:
    # codex に pytest を依頼するようなコマンドは CLI として扱う
    assert fd.classify_command('codex exec "run pytest"') == fd.KIND_CLI


def test_classify_command_shell_and_empty() -> None:
    assert fd.classify_command("ls -la") == fd.KIND_SHELL
    assert fd.classify_command("") == fd.KIND_NONE


# ---------------------------------------------------------------------------
# has_failure_markers
# ---------------------------------------------------------------------------


def test_failure_markers_detects_pytest_failure() -> None:
    assert fd.has_failure_markers("===== 1 failed, 3 passed =====") is True
    assert fd.has_failure_markers("FAILED tests/test_x.py::test_y") is True
    assert fd.has_failure_markers("E   AssertionError: boom") is True


def test_failure_markers_ignores_clean_runs() -> None:
    assert fd.has_failure_markers("===== 4 passed in 0.1s =====") is False
    assert fd.has_failure_markers("0 failed") is False
    assert fd.has_failure_markers("") is False


def test_failure_markers_detects_ruff_mypy_errors() -> None:
    assert fd.has_failure_markers("Found 2 errors.") is True
    assert fd.has_failure_markers("Found 0 errors") is False


# ---------------------------------------------------------------------------
# classify_error
# ---------------------------------------------------------------------------


def test_classify_error_by_exit_code_timeout() -> None:
    assert fd.classify_error("", fd.TIMEOUT_EXIT_CODE) == "timeout"


def test_classify_error_by_output() -> None:
    assert fd.classify_error("Request timed out", 1) == "timeout"
    assert fd.classify_error("401 Unauthorized", 1) == "auth"
    assert fd.classify_error("command not found", 127) == "not_found"
    assert fd.classify_error("429 rate limit exceeded", 1) == "rate_limit"
    assert fd.classify_error("SyntaxError: invalid syntax", 1) == "syntax"
    assert fd.classify_error("AssertionError", 1) == "assertion"
    assert fd.classify_error("something odd", 1) == "unknown"


# ---------------------------------------------------------------------------
# analyze — Bash: exit_code 判定
# ---------------------------------------------------------------------------


def test_analyze_bash_nonzero_exit_is_failure() -> None:
    result = fd.analyze("Bash", {"command": "ls /nope"}, {"exit_code": 2, "stdout": "no such file"})
    assert result is not None
    assert result["failure_type"] == fd.FAILURE_TOOL_ERROR
    assert result["detected_by"] == fd.BY_EXIT_CODE
    assert result["error_type"] == "not_found"


def test_analyze_bash_test_command_nonzero_is_test_failure() -> None:
    result = fd.analyze("Bash", {"command": "pytest"}, {"exit_code": 1, "stdout": "1 failed"})
    assert result["failure_type"] == fd.FAILURE_TEST
    assert result["command_kind"] == fd.KIND_TEST


def test_analyze_bash_cli_command_nonzero_is_cli_failure() -> None:
    result = fd.analyze("Bash", {"command": "codex exec 'q'"}, {"exit_code": 1, "stdout": ""})
    assert result["failure_type"] == fd.FAILURE_CLI


def test_analyze_bash_clean_run_is_none() -> None:
    assert fd.analyze("Bash", {"command": "pytest"}, {"exit_code": 0, "stdout": "4 passed"}) is None
    assert fd.analyze("Bash", {"command": "ls"}, {"exit_code": 0, "stdout": "a b"}) is None


# ---------------------------------------------------------------------------
# analyze — Bash: 出力パターン判定（パイプマスク・exit_code None）
# ---------------------------------------------------------------------------


def test_analyze_bash_pipe_masked_failure_detected_by_output() -> None:
    # `pytest ... | tail` で exit_code が 0 にマスクされても失敗を拾う
    result = fd.analyze(
        "Bash",
        {"command": "pytest tests/ | tail -30"},
        {"exit_code": 0, "stdout": "FAILED tests/test_x.py::test_y\n1 failed, 5 passed"},
    )
    assert result is not None
    assert result["failure_type"] == fd.FAILURE_TEST
    assert result["detected_by"] == fd.BY_OUTPUT_PATTERN


def test_analyze_bash_exit_code_none_with_markers() -> None:
    # exit_code が欠落していても test/lint なら出力で判定
    result = fd.analyze(
        "Bash",
        {"command": "ruff check ."},
        {"stdout": "Found 3 errors."},
    )
    assert result is not None
    assert result["failure_type"] == fd.FAILURE_LINT
    assert result["detected_by"] == fd.BY_OUTPUT_PATTERN


def test_analyze_bash_exit_code_none_clean_is_none() -> None:
    # exit_code 欠落 + 失敗マーカーなし → 誤検知しない
    assert fd.analyze("Bash", {"command": "pytest"}, {"stdout": "4 passed"}) is None


def test_analyze_bash_shell_command_output_markers_ignored() -> None:
    # 非 test/lint の一般コマンドは出力パターン判定の対象外（誤検知防止）
    assert (
        fd.analyze("Bash", {"command": "cat log.txt"}, {"exit_code": 0, "stdout": "1 failed"})
        is None
    )


# ---------------------------------------------------------------------------
# analyze — 非 Bash ツール
# ---------------------------------------------------------------------------


def test_analyze_tool_error_field() -> None:
    result = fd.analyze("Edit", {}, {"error": "String to replace not found"})
    assert result is not None
    assert result["failure_type"] == fd.FAILURE_TOOL_ERROR
    assert result["detected_by"] == fd.BY_TOOL_ERROR


def test_analyze_tool_is_error_flag() -> None:
    result = fd.analyze("Write", {}, {"is_error": True, "content": "permission denied"})
    assert result["failure_type"] == fd.FAILURE_TOOL_ERROR
    assert result["error_type"] == "auth"


def test_analyze_tool_success_is_none() -> None:
    assert fd.analyze("Edit", {}, {"content": "ok"}) is None
    assert fd.analyze("Write", {}, {}) is None


def test_analyze_handles_missing_dicts() -> None:
    assert fd.analyze("Bash", None, None) is None
