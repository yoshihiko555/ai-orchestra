"""hook_utils.py の追加ユニットテスト。"""

from __future__ import annotations

import os
import subprocess
import sys

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
            (
                '"${AI_ORCHESTRA_PYTHON:-python3}" '
                '"$AI_ORCHESTRA_DIR/packages/core/hooks/check-plan-gate.py"',
                True,
            ),
            (
                '"${AI_ORCHESTRA_PYTHON:-python3}" '
                '"$AI_ORCHESTRA_DIR/packages/core/scripts/check-plan-gate.py"',
                False,
            ),
            ('/usr/bin/python3 "$AI_ORCHESTRA_DIR/packages/core/hooks/check-plan-gate.py"', False),
        ],
        ids=[
            "legacy_valid_hook",
            "legacy_non_hook_path",
            "plain_python_path",
            "current_valid_hook",
            "current_non_hook_path",
            "unknown_interpreter",
        ],
    )
    def test_detects_orchestra_hook_pattern(self, command: str, expected: bool) -> None:
        """AI Orchestra の hook パスかどうかを判定する。"""
        assert hook_utils.is_orchestra_hook(command) is expected


class TestParsePkgFromCommand:
    """parse_pkg_from_command のテスト。"""

    def test_returns_package_name_for_valid_hook_command(self) -> None:
        """正常な hook コマンドから package 名を抽出する（旧表記）。"""
        command = 'python3 "$AI_ORCHESTRA_DIR/packages/quality-gates/hooks/test-gate-checker.py"'
        assert hook_utils.parse_pkg_from_command(command) == "quality-gates"

    def test_returns_package_name_for_current_interpreter_command(self) -> None:
        """現行表記（AI_ORCHESTRA_PYTHON 参照）でも package 名を抽出する。"""
        command = hook_utils.get_hook_command("quality-gates", "test-gate-checker.py")
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


class TestHookInterpreterResolution:
    """hook コマンドのインタプリタ解決のテスト（Issue #343）。"""

    def test_generated_command_references_override_env_var(self) -> None:
        """生成コマンドは AI_ORCHESTRA_PYTHON を参照し python3 にフォールバックする。"""
        command = hook_utils.get_hook_command("core", "check-plan-gate.py")

        assert command.startswith('"${AI_ORCHESTRA_PYTHON:-python3}" ')
        assert command.endswith('"$AI_ORCHESTRA_DIR/packages/core/hooks/check-plan-gate.py"')

    def test_sync_hook_command_uses_same_interpreter(self) -> None:
        """sync-orchestra hook も同じインタプリタ参照を使う。"""
        assert hook_utils.SYNC_HOOK_COMMAND.startswith(hook_utils.HOOK_INTERPRETER + " ")
        assert hook_utils.is_sync_hook_command(hook_utils.SYNC_HOOK_COMMAND) is True

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ('python3 "$AI_ORCHESTRA_DIR/scripts/sync-orchestra.py"', True),
            ('python3 "$AI_ORCHESTRA_DIR/packages/core/hooks/check-plan-gate.py"', False),
            ("python3 /tmp/sync-orchestra.py", False),
        ],
        ids=["legacy_sync_hook", "package_hook", "unrelated_script"],
    )
    def test_is_sync_hook_command_accepts_legacy_form(self, command: str, expected: bool) -> None:
        """旧表記の sync hook も検出できる（移行判定に必要）。"""
        assert hook_utils.is_sync_hook_command(command) is expected

    def _run_in_shell(self, command: str, env: dict[str, str]) -> str:
        """生成された hook コマンド文字列を sh 経由で実行し stdout を返す。"""
        # PATH は検証用シムだけに絞るため、シェル自体は絶対パスで起動する
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def test_shell_expansion_prefers_env_var_over_path(self, tmp_path) -> None:
        """AI_ORCHESTRA_PYTHON が設定されていれば PATH の python3 より優先される。"""
        script = tmp_path / "print-exe.py"
        script.write_text("import sys; print(sys.executable)\n", encoding="utf-8")
        # PATH 上の python3 は必ず失敗するシムに差し替える（誤って使えば検知できる）
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        shim = shim_dir / "python3"
        shim.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
        shim.chmod(0o755)
        command = f'"${{AI_ORCHESTRA_PYTHON:-python3}}" "{script}"'

        stdout = self._run_in_shell(
            command,
            {**os.environ, "PATH": str(shim_dir), "AI_ORCHESTRA_PYTHON": sys.executable},
        )

        assert stdout == sys.executable

    def test_shell_expansion_falls_back_to_path_python3(self, tmp_path) -> None:
        """AI_ORCHESTRA_PYTHON 未設定時は従来どおり PATH の python3 を使う（後方互換）。"""
        script = tmp_path / "print-exe.py"
        script.write_text("import sys; print(sys.executable)\n", encoding="utf-8")
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        shim = shim_dir / "python3"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        command = f'"${{AI_ORCHESTRA_PYTHON:-python3}}" "{script}"'
        env = {k: v for k, v in os.environ.items() if k != "AI_ORCHESTRA_PYTHON"}

        stdout = self._run_in_shell(command, {**env, "PATH": str(shim_dir)})

        assert stdout == sys.executable


class TestMigrateHookInterpreters:
    """migrate_hook_interpreters のテスト（Issue #343）。"""

    def _entry(self, *commands: str, matcher: str | None = None) -> dict:
        entry: dict = {"hooks": [{"type": "command", "command": c, "timeout": 5} for c in commands]}
        if matcher:
            entry["matcher"] = matcher
        return entry

    def test_migrates_package_hook_under_matcher(self) -> None:
        """sync hook 以外のパッケージ hook も matcher 付きイベントで移行される。

        install/enable と SessionStart 同期の両方で、旧表記が現行表記へ揃うことを保証する
        （揃わないと新旧が並んで PostToolUse のフォーマッタ等が二重起動する）。
        """
        legacy = 'python3 "$AI_ORCHESTRA_DIR/packages/codd/hooks/codd-scan-postedit.py"'
        settings_hooks = {"PostToolUse": [self._entry(legacy, matcher="Edit|Write")]}

        changed = hook_utils.migrate_hook_interpreters(settings_hooks)

        commands = [h["command"] for h in settings_hooks["PostToolUse"][0]["hooks"]]
        assert changed == 1
        assert commands == [hook_utils.get_hook_command("codd", "codd-scan-postedit.py")]

    def test_collapses_legacy_and_current_duplicates(self) -> None:
        """新旧表記が併存するパッケージ hook は 1 件に畳む（二重起動の防止）。"""
        current = hook_utils.get_hook_command("codd", "codd-scan-postedit.py")
        legacy = 'python3 "$AI_ORCHESTRA_DIR/packages/codd/hooks/codd-scan-postedit.py"'
        settings_hooks = {"PostToolUse": [self._entry(legacy, current, matcher="Edit|Write")]}

        changed = hook_utils.migrate_hook_interpreters(settings_hooks)

        commands = [h["command"] for h in settings_hooks["PostToolUse"][0]["hooks"]]
        # 旧表記の書き換えと重複エントリの除去で 2 件を数える
        assert changed == 2
        assert commands == [current]

    def test_leaves_foreign_hooks_untouched(self) -> None:
        """利用者自身が登録した python3 hook は書き換えない。"""
        foreign = 'python3 "$HOME/my/hook.py"'
        other = "node /opt/tools/lint.js"
        settings_hooks = {"PreToolUse": [self._entry(foreign, other)]}

        changed = hook_utils.migrate_hook_interpreters(settings_hooks)

        commands = [h["command"] for h in settings_hooks["PreToolUse"][0]["hooks"]]
        assert changed == 0
        assert commands == [foreign, other]

    def test_keeps_separate_matchers_independent(self) -> None:
        """matcher が異なるエントリ間では畳まない（別々の発火条件を維持する）。"""
        legacy = 'python3 "$AI_ORCHESTRA_DIR/packages/codd/hooks/codd-scan-postedit.py"'
        settings_hooks = {
            "PostToolUse": [
                self._entry(legacy, matcher="Edit|Write"),
                self._entry(legacy, matcher="Bash"),
            ]
        }

        changed = hook_utils.migrate_hook_interpreters(settings_hooks)

        current = hook_utils.get_hook_command("codd", "codd-scan-postedit.py")
        assert changed == 2
        assert [h["command"] for h in settings_hooks["PostToolUse"][0]["hooks"]] == [current]
        assert [h["command"] for h in settings_hooks["PostToolUse"][1]["hooks"]] == [current]

    def test_is_noop_when_already_current(self) -> None:
        """現行表記だけの settings は変更しない（無駄な差分・書き込みを出さない）。"""
        settings_hooks = {
            "SessionStart": [self._entry(hook_utils.SYNC_HOOK_COMMAND)],
            "PostToolUse": [
                self._entry(
                    hook_utils.get_hook_command("codd", "codd-scan-postedit.py"),
                    matcher="Edit|Write",
                )
            ],
        }

        assert hook_utils.migrate_hook_interpreters(settings_hooks) == 0

    def test_tolerates_malformed_settings_shapes(self) -> None:
        """壊れた形の settings でも例外を投げない（同期を止めないため）。"""
        settings_hooks = {"SessionStart": "not-a-list", "PreToolUse": ["not-a-dict"]}

        assert hook_utils.migrate_hook_interpreters(settings_hooks) == 0

    @pytest.mark.parametrize(
        "command",
        [
            "python3 /tmp/local-hook.py",
            'python3 "$AI_ORCHESTRA_DIR/packages/core/scripts/not-a-hook.py"',
            "node /opt/tools/lint.js",
        ],
        ids=["absolute_path", "non_hooks_dir", "non_python"],
    )
    def test_canonical_hook_command_rejects_foreign_commands(self, command: str) -> None:
        """ai-orchestra 由来でないコマンドは正規化対象にしない。"""
        assert hook_utils.canonical_hook_command(command) is None
