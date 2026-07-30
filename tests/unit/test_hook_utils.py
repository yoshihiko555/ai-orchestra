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
        """文字列指定は matcher なし・既定 timeout として扱う。"""
        assert hook_utils.parse_hook_entry("sync-orchestra.py") == (
            "sync-orchestra.py",
            None,
            hook_utils.DEFAULT_HOOK_TIMEOUT,
        )

    def test_dict_value_returns_file_and_matcher(self) -> None:
        """辞書指定から file と matcher を取り出す（timeout 未指定は既定値）。"""
        value = {"file": "check-plan-gate.py", "matcher": "Task"}
        assert hook_utils.parse_hook_entry(value) == (
            "check-plan-gate.py",
            "Task",
            hook_utils.DEFAULT_HOOK_TIMEOUT,
        )

    def test_dict_without_matcher_returns_none_matcher(self) -> None:
        """matcher がない辞書は None を返す。"""
        assert hook_utils.parse_hook_entry({"file": "sync-orchestra.py"}) == (
            "sync-orchestra.py",
            None,
            hook_utils.DEFAULT_HOOK_TIMEOUT,
        )

    def test_unsupported_value_returns_empty_defaults(self) -> None:
        """未対応型は空文字・None・既定 timeout を返す。"""
        assert hook_utils.parse_hook_entry(123) == ("", None, hook_utils.DEFAULT_HOOK_TIMEOUT)

    def test_dict_with_valid_timeout_returns_it(self) -> None:
        """正の int の timeout はそのまま採用する。"""
        value = {"file": "codd-scan-postedit.py", "matcher": "Edit|Write", "timeout": 90}
        assert hook_utils.parse_hook_entry(value) == ("codd-scan-postedit.py", "Edit|Write", 90)

    @pytest.mark.parametrize(
        "bad_timeout",
        [0, -1, "90", 1.5, True],
        ids=["zero", "negative", "string", "float", "bool"],
    )
    def test_dict_with_invalid_timeout_falls_back_to_default(self, bad_timeout: object) -> None:
        """不正な timeout（0以下・非int・bool）は既定値にフォールバックする。"""
        value = {"file": "hook.py", "timeout": bad_timeout}
        assert hook_utils.parse_hook_entry(value) == (
            "hook.py",
            None,
            hook_utils.DEFAULT_HOOK_TIMEOUT,
        )


class TestAddHookToSettingsTimeout:
    """add_hook_to_settings の timeout 反映・更新のテスト。"""

    def test_new_registration_uses_given_timeout(self) -> None:
        """新規登録時は指定 timeout が settings に反映される。"""
        settings_hooks: dict = {}

        changed = hook_utils.add_hook_to_settings(
            settings_hooks, "PostToolUse", "cmd", matcher="Edit|Write", timeout=90
        )

        hook = settings_hooks["PostToolUse"][0]["hooks"][0]
        assert changed is True
        assert hook["timeout"] == 90

    def test_new_registration_defaults_to_five_when_unspecified(self) -> None:
        """timeout 未指定の新規登録は既存互換の既定値 5 になる。"""
        settings_hooks: dict = {}

        hook_utils.add_hook_to_settings(settings_hooks, "SessionStart", "cmd")

        hook = settings_hooks["SessionStart"][0]["hooks"][0]
        assert hook["timeout"] == hook_utils.DEFAULT_HOOK_TIMEOUT

    def test_existing_entry_with_same_timeout_is_noop(self) -> None:
        """既に同じ timeout で登録済みの場合は変更なし（False を返す）。"""
        settings_hooks: dict = {}
        hook_utils.add_hook_to_settings(settings_hooks, "PostToolUse", "cmd", timeout=90)

        changed = hook_utils.add_hook_to_settings(settings_hooks, "PostToolUse", "cmd", timeout=90)

        assert changed is False

    def test_existing_entry_with_different_timeout_is_updated(self) -> None:
        """登録済みの timeout が manifest 側と異なる場合は更新し True を返す。"""
        settings_hooks: dict = {}
        hook_utils.add_hook_to_settings(settings_hooks, "PostToolUse", "cmd", timeout=5)

        changed = hook_utils.add_hook_to_settings(settings_hooks, "PostToolUse", "cmd", timeout=90)

        hook = settings_hooks["PostToolUse"][0]["hooks"][0]
        assert changed is True
        assert hook["timeout"] == 90
        # 他の属性（コマンド・type）は保たれる
        assert hook["command"] == "cmd"
        assert hook["type"] == "command"
