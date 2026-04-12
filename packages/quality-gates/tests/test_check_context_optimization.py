from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.module_loader import load_module

check_context_optimization = load_module(
    "check_context_optimization",
    "packages/quality-gates/hooks/check-context-optimization.py",
)


def _settings(**overrides) -> dict:
    base = {"enabled": True, "read_line_threshold": 200, "max_file_size_bytes": 5_242_880}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------


def test_safe_int_valid() -> None:
    assert check_context_optimization._safe_int(200, 100) == 200


def test_safe_int_invalid_falls_back() -> None:
    assert check_context_optimization._safe_int("bad", 100) == 100
    assert check_context_optimization._safe_int(None, 100) == 100


def test_safe_int_below_minimum_falls_back() -> None:
    assert check_context_optimization._safe_int(0, 100) == 100
    assert check_context_optimization._safe_int(-5, 100) == 100


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------


def test_is_enabled_default_true_when_missing() -> None:
    assert check_context_optimization.is_enabled({}) is True


def test_is_enabled_explicit_false() -> None:
    assert check_context_optimization.is_enabled({"enabled": False}) is False


# ---------------------------------------------------------------------------
# check_read
# ---------------------------------------------------------------------------


def test_check_read_returns_empty_when_file_missing(tmp_path: Path) -> None:
    msg = check_context_optimization.check_read(
        {"file_path": str(tmp_path / "missing.txt")}, _settings()
    )
    assert msg == ""


def test_check_read_returns_empty_when_offset_only(tmp_path: Path) -> None:
    target = tmp_path / "big.txt"
    target.write_text("\n".join(str(i) for i in range(500)))
    msg = check_context_optimization.check_read(
        {"file_path": str(target), "offset": 1}, _settings()
    )
    assert msg == ""


def test_check_read_returns_empty_when_limit_only(tmp_path: Path) -> None:
    target = tmp_path / "big.txt"
    target.write_text("\n".join(str(i) for i in range(500)))
    msg = check_context_optimization.check_read(
        {"file_path": str(target), "limit": 50}, _settings()
    )
    assert msg == ""


def test_check_read_returns_empty_for_small_file(tmp_path: Path) -> None:
    target = tmp_path / "small.txt"
    target.write_text("\n".join(str(i) for i in range(50)))
    msg = check_context_optimization.check_read({"file_path": str(target)}, _settings())
    assert msg == ""


def test_check_read_suggests_for_large_file(tmp_path: Path) -> None:
    target = tmp_path / "large.txt"
    target.write_text("\n".join(str(i) for i in range(500)))
    msg = check_context_optimization.check_read({"file_path": str(target)}, _settings())
    assert "[Context Optimization]" in msg
    assert "offset/limit" in msg
    assert "500 行" in msg


def test_check_read_skipped_when_file_too_large(tmp_path: Path) -> None:
    target = tmp_path / "huge.txt"
    target.write_text("\n".join(str(i) for i in range(500)))
    msg = check_context_optimization.check_read(
        {"file_path": str(target)},
        _settings(max_file_size_bytes=10),
    )
    assert msg == ""


def test_check_read_skipped_for_non_regular_file(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    try:
        os.mkfifo(fifo)
    except (AttributeError, OSError) as exc:
        pytest.skip(f"mkfifo not supported on this platform: {exc}")
    msg = check_context_optimization.check_read({"file_path": str(fifo)}, _settings())
    assert msg == ""


# ---------------------------------------------------------------------------
# check_grep
# ---------------------------------------------------------------------------


def test_check_grep_skipped_for_files_with_matches() -> None:
    msg = check_context_optimization.check_grep(
        {"output_mode": "files_with_matches", "pattern": "foo"}, _settings()
    )
    assert msg == ""


def test_check_grep_skipped_when_head_limit_set() -> None:
    msg = check_context_optimization.check_grep(
        {"output_mode": "content", "pattern": "foo", "head_limit": 30}, _settings()
    )
    assert msg == ""


def test_check_grep_suggests_for_unbounded_content_mode() -> None:
    msg = check_context_optimization.check_grep(
        {"output_mode": "content", "pattern": "foo"}, _settings()
    )
    assert "[Context Optimization]" in msg
    assert "head_limit" in msg
    assert "count" in msg


def test_check_grep_truncates_long_pattern() -> None:
    long_pattern = "x" * 200
    msg = check_context_optimization.check_grep(
        {"output_mode": "content", "pattern": long_pattern}, _settings()
    )
    assert "..." in msg
    assert long_pattern not in msg


# ---------------------------------------------------------------------------
# check_bash
# ---------------------------------------------------------------------------


def test_check_bash_suggests_read_for_cat() -> None:
    msg = check_context_optimization.check_bash({"command": "cat src/foo.py"}, _settings())
    assert "Read" in msg
    assert "cat" in msg


def test_check_bash_suggests_glob_for_find() -> None:
    msg = check_context_optimization.check_bash({"command": "find . -name '*.py'"}, _settings())
    assert "Glob" in msg


def test_check_bash_suggests_grep_for_rg() -> None:
    msg = check_context_optimization.check_bash({"command": "rg --files src/"}, _settings())
    assert "Grep" in msg


def test_check_bash_handles_sudo_prefix() -> None:
    msg = check_context_optimization.check_bash({"command": "sudo cat /etc/hosts"}, _settings())
    assert "Read" in msg


def test_check_bash_handles_chained_wrapper_prefixes() -> None:
    msg = check_context_optimization.check_bash(
        {"command": "sudo nice cat /etc/hosts"}, _settings()
    )
    assert "Read" in msg


def test_check_bash_returns_empty_when_only_wrappers() -> None:
    msg = check_context_optimization.check_bash({"command": "sudo nice"}, _settings())
    assert msg == ""


def test_check_bash_returns_empty_for_unknown_command() -> None:
    msg = check_context_optimization.check_bash({"command": "git status"}, _settings())
    assert msg == ""


def test_check_bash_returns_empty_for_invalid_shell_syntax() -> None:
    msg = check_context_optimization.check_bash({"command": "cat 'unterminated"}, _settings())
    assert msg == ""


def test_check_bash_returns_empty_for_blank_command() -> None:
    msg = check_context_optimization.check_bash({"command": ""}, _settings())
    assert msg == ""


# ---------------------------------------------------------------------------
# _sanitize_for_message
# ---------------------------------------------------------------------------


def test_sanitize_strips_control_characters() -> None:
    result = check_context_optimization._sanitize_for_message("foo\nbar\rbaz\x00qux")
    assert "\n" not in result
    assert "\r" not in result
    assert "\x00" not in result
    assert "foo" in result and "qux" in result


def test_sanitize_truncates_long_strings() -> None:
    result = check_context_optimization._sanitize_for_message("x" * 500, max_len=20)
    assert len(result) == 20
    assert result.endswith("...")


def test_check_grep_sanitizes_pattern_with_newline() -> None:
    msg = check_context_optimization.check_grep(
        {"output_mode": "content", "pattern": "foo\nbar"},
        _settings(),
    )
    pattern_line_count = sum(1 for line in msg.split("\n") if "pattern:" in line)
    assert pattern_line_count == 1
    assert "foo bar" in msg or "'foo bar'" in msg
