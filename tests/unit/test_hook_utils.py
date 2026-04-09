"""hook_utils.py の追加ユニットテスト。"""

from __future__ import annotations

import pytest

from tests.module_loader import load_module

hook_utils = load_module("hook_utils_test", "scripts/lib/hook_utils.py")


class TestIsOrchestraHook:
    """is_orchestra_hook のテスト。"""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ('python3 "$AI_ORCHESTRA_DIR/packages/core/hooks/check-plan-gate.py"', True),
            ('python3 "$AI_ORCHESTRA_DIR/packages/core/scripts/check-plan-gate.py"', False),
            ("python3 /tmp/local-hook.py", False),
        ],
        ids=["valid_hook", "non_hook_path", "plain_python_path"],
    )
    def test_detects_orchestra_hook_pattern(self, command: str, expected: bool) -> None:
        """AI Orchestra の hook パスかどうかを判定する。"""
        assert hook_utils.is_orchestra_hook(command) is expected


class TestParsePkgFromCommand:
    """parse_pkg_from_command のテスト。"""

    def test_returns_package_name_for_valid_hook_command(self) -> None:
        """正常な hook コマンドから package 名を抽出する。"""
        command = 'python3 "$AI_ORCHESTRA_DIR/packages/quality-gates/hooks/test-gate-checker.py"'
        assert hook_utils.parse_pkg_from_command(command) == "quality-gates"

    @pytest.mark.parametrize(
        "command",
        [
            'python3 "$AI_ORCHESTRA_DIR/scripts/sync-orchestra.py"',
            'python3 "$AI_ORCHESTRA_DIR/packages"',
        ],
        ids=["invalid_prefix", "missing_package_separator"],
    )
    def test_returns_none_for_non_package_hook_command(self, command: str) -> None:
        """package 名を抽出できない形式では None を返す。"""
        assert hook_utils.parse_pkg_from_command(command) is None


class TestParseHookEntry:
    """parse_hook_entry のテスト。"""

    def test_string_value_returns_file_and_none(self) -> None:
        """文字列指定は matcher なしとして扱う。"""
        assert hook_utils.parse_hook_entry("sync-orchestra.py") == ("sync-orchestra.py", None)

    def test_dict_value_returns_file_and_matcher(self) -> None:
        """辞書指定から file と matcher を取り出す。"""
        value = {"file": "check-plan-gate.py", "matcher": "Task"}
        assert hook_utils.parse_hook_entry(value) == ("check-plan-gate.py", "Task")

    def test_dict_without_matcher_returns_none_matcher(self) -> None:
        """matcher がない辞書は None を返す。"""
        assert hook_utils.parse_hook_entry({"file": "sync-orchestra.py"}) == (
            "sync-orchestra.py",
            None,
        )

    def test_unsupported_value_returns_empty_defaults(self) -> None:
        """未対応型は空文字と None を返す。"""
        assert hook_utils.parse_hook_entry(123) == ("", None)
