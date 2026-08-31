"""orchestra_hooks.py の HooksMixin テスト。

OrchestraManager 経由で HooksMixin のメソッドをテストする。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

manager_mod = load_module("orchestra_manager", "scripts/orchestra-manager.py")
OrchestraManager = manager_mod.OrchestraManager
models_mod = load_module("orchestra_models", "scripts/lib/orchestra_models.py")
HookEntry = models_mod.HookEntry
Package = models_mod.Package
hooks_mod = sys.modules[manager_mod.HooksMixin.__module__]
hook_utils = load_module("hook_utils_for_hooks_test", "scripts/lib/hook_utils.py")


def _make_manager(tmp_path: Path) -> OrchestraManager:
    """テスト用 OrchestraManager を生成する。"""
    (tmp_path / "packages").mkdir(parents=True, exist_ok=True)
    return OrchestraManager(tmp_path)


def _make_package(tmp_path: Path, name: str = "mypkg", hooks: dict | None = None) -> Package:
    """テスト用 Package を manifest.json 経由で生成する。"""
    pkg_dir = tmp_path / "packages" / name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "description": "test",
        "hooks": hooks or {},
    }
    manifest_path = pkg_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return Package.load(manifest_path)


def _write_launchable_python(path: Path) -> Path:
    """起動プローブを通る python の代役を作る。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _pin_venv_interpreter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    executable: Path,
    prefix: Path,
    base_executable: str | None,
) -> None:
    """venv の中から実行している状態を密閉して再現する（Issue #343）。

    書き込み候補の選定は `sys.executable` / `sys.prefix` / `sys.base_prefix` /
    `sys._base_executable` の 4 つで決まる。pytest 自身がどこで走っていても結果が変わらない
    よう、4 つとも明示的に与える。`base_executable=None` は `sys._base_executable` を持たない
    ビルド（属性なし）を再現する。
    """
    monkeypatch.setattr(hooks_mod.sys, "executable", str(executable))
    monkeypatch.setattr(hooks_mod.sys, "prefix", str(prefix))
    monkeypatch.setattr(hooks_mod.sys, "base_prefix", str(prefix.parent / "base-prefix"))
    if base_executable is None:
        monkeypatch.delattr(hooks_mod.sys, "_base_executable", raising=False)
        return
    monkeypatch.setattr(hooks_mod.sys, "_base_executable", base_executable, raising=False)


class TestCountRegisteredHooks:
    def test_no_hooks_returns_zero_zero(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        pkg = _make_package(tmp_path, hooks={})
        settings: dict = {"hooks": {}}

        # Act
        result = manager._count_registered_hooks(pkg, settings)

        # Assert
        assert result == (0, 0)

    def test_some_hooks_registered_returns_correct_count(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        pkg = _make_package(
            tmp_path,
            hooks={"SessionStart": ["hook_a.py", "hook_b.py"]},
        )
        settings: dict = {"hooks": {}}
        # Register only hook_a.py
        manager.add_hook_to_settings(settings, "SessionStart", "hook_a.py", "mypkg")

        # Act
        registered, total = manager._count_registered_hooks(pkg, settings)

        # Assert
        assert total == 2
        assert registered == 1

    def test_all_hooks_registered_returns_total_total(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        pkg = _make_package(
            tmp_path,
            hooks={"SessionStart": ["hook_a.py", "hook_b.py"]},
        )
        settings: dict = {"hooks": {}}
        manager.add_hook_to_settings(settings, "SessionStart", "hook_a.py", "mypkg")
        manager.add_hook_to_settings(settings, "SessionStart", "hook_b.py", "mypkg")

        # Act
        registered, total = manager._count_registered_hooks(pkg, settings)

        # Assert
        assert registered == total == 2


class TestApplyHooks:
    def test_add_action_registers_hooks(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        pkg = _make_package(tmp_path, hooks={"SessionStart": ["hook_a.py"]})
        settings: dict = {"hooks": {}}

        # Act
        manager._apply_hooks(pkg, settings, action="add")

        # Assert
        assert manager.is_hook_registered(settings, "SessionStart", "hook_a.py", "mypkg")

    def test_remove_action_removes_hooks(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        pkg = _make_package(tmp_path, hooks={"SessionStart": ["hook_a.py"]})
        settings: dict = {"hooks": {}}
        manager.add_hook_to_settings(settings, "SessionStart", "hook_a.py", "mypkg")
        assert manager.is_hook_registered(settings, "SessionStart", "hook_a.py", "mypkg")

        # Act
        manager._apply_hooks(pkg, settings, action="remove")

        # Assert
        assert not manager.is_hook_registered(settings, "SessionStart", "hook_a.py", "mypkg")

    def test_add_migrates_legacy_interpreter_instead_of_duplicating(self, tmp_path: Path) -> None:
        """旧表記が残るプロジェクトへ install/enable しても hook は二重登録されない。

        登録済み判定はコマンド文字列の完全一致で行うため、移行しないと旧表記を残したまま
        新表記が追加され、次の SessionStart 同期で prune されるまで hook が 2 回走る。
        """
        manager = _make_manager(tmp_path)
        pkg = _make_package(tmp_path, hooks={"SessionStart": ["hook_a.py"]})
        legacy_command = 'python3 "$AI_ORCHESTRA_DIR/packages/mypkg/hooks/hook_a.py"'
        settings: dict = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": legacy_command, "timeout": 5}]}
                ]
            }
        }

        manager._apply_hooks(pkg, settings, action="add")

        commands = [h["command"] for h in settings["hooks"]["SessionStart"][0]["hooks"]]
        assert commands == [hook_utils.get_hook_command("mypkg", "hook_a.py")]

    def test_remove_also_removes_legacy_interpreter_hook(self, tmp_path: Path) -> None:
        """旧表記で登録された hook も uninstall/disable で取りこぼさず削除する。"""
        manager = _make_manager(tmp_path)
        pkg = _make_package(tmp_path, hooks={"SessionStart": ["hook_a.py"]})
        legacy_command = 'python3 "$AI_ORCHESTRA_DIR/packages/mypkg/hooks/hook_a.py"'
        settings: dict = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": legacy_command, "timeout": 5}]}
                ]
            }
        }

        manager._apply_hooks(pkg, settings, action="remove")

        assert settings["hooks"]["SessionStart"] == []

    def test_dry_run_does_not_migrate_legacy_interpreter(self, tmp_path: Path) -> None:
        """dry-run では移行も含めて settings を書き換えない。"""
        manager = _make_manager(tmp_path)
        pkg = _make_package(tmp_path, hooks={"SessionStart": ["hook_a.py"]})
        legacy_command = 'python3 "$AI_ORCHESTRA_DIR/packages/mypkg/hooks/hook_a.py"'
        settings: dict = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": legacy_command, "timeout": 5}]}
                ]
            }
        }

        manager._apply_hooks(pkg, settings, action="add", dry_run=True)

        commands = [h["command"] for h in settings["hooks"]["SessionStart"][0]["hooks"]]
        assert commands == [legacy_command]

    def test_dry_run_does_not_mutate_settings(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        pkg = _make_package(tmp_path, hooks={"SessionStart": ["hook_a.py"]})
        settings: dict = {"hooks": {}}

        # Act
        manager._apply_hooks(pkg, settings, action="add", dry_run=True)

        # Assert
        assert not manager.is_hook_registered(settings, "SessionStart", "hook_a.py", "mypkg")

    def test_dry_run_prints_hook_register_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        pkg = _make_package(tmp_path, hooks={"SessionStart": ["hook_a.py"]})
        settings: dict = {"hooks": {}}

        # Act
        manager._apply_hooks(pkg, settings, action="add", dry_run=True)

        # Assert
        captured = capsys.readouterr()
        assert "フック登録" in captured.out

    def test_dry_run_prints_hook_remove_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        pkg = _make_package(tmp_path, hooks={"SessionStart": ["hook_a.py"]})
        settings: dict = {"hooks": {}}

        # Act
        manager._apply_hooks(pkg, settings, action="remove", dry_run=True)

        # Assert
        captured = capsys.readouterr()
        assert "フック削除" in captured.out


class TestAddHookToSettings:
    def test_creates_new_event_entry_when_absent(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        settings: dict = {}

        # Act
        manager.add_hook_to_settings(settings, "SessionStart", "hook.py", "mypkg")

        # Assert
        assert "SessionStart" in settings["hooks"]
        assert len(settings["hooks"]["SessionStart"]) == 1

    def test_idempotent_on_duplicate_command(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        settings: dict = {"hooks": {}}

        # Act
        manager.add_hook_to_settings(settings, "SessionStart", "hook.py", "mypkg")
        manager.add_hook_to_settings(settings, "SessionStart", "hook.py", "mypkg")

        # Assert
        hooks_in_entry = settings["hooks"]["SessionStart"][0]["hooks"]
        assert len(hooks_in_entry) == 1

    def test_with_matcher_creates_matcher_entry(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        settings: dict = {"hooks": {}}

        # Act
        manager.add_hook_to_settings(
            settings, "PreToolUse", "hook.py", "mypkg", matcher="Edit|Write"
        )

        # Assert
        entry = settings["hooks"]["PreToolUse"][0]
        assert entry.get("matcher") == "Edit|Write"

    def test_without_matcher_skips_matcher_entries(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        settings: dict = {"hooks": {}}

        # Act
        manager.add_hook_to_settings(settings, "SessionStart", "hook.py", "mypkg")

        # Assert
        entry = settings["hooks"]["SessionStart"][0]
        assert "matcher" not in entry


class TestRemoveHookFromSettings:
    def test_noop_when_event_absent(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        settings: dict = {"hooks": {}}

        # Act & Assert (no exception)
        manager.remove_hook_from_settings(settings, "SessionStart", "hook.py", "mypkg")

    def test_removes_target_command_only(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        settings: dict = {"hooks": {}}
        manager.add_hook_to_settings(settings, "SessionStart", "hook_a.py", "mypkg")
        manager.add_hook_to_settings(settings, "SessionStart", "hook_b.py", "mypkg")

        # Act
        manager.remove_hook_from_settings(settings, "SessionStart", "hook_a.py", "mypkg")

        # Assert
        assert not manager.is_hook_registered(settings, "SessionStart", "hook_a.py", "mypkg")
        assert manager.is_hook_registered(settings, "SessionStart", "hook_b.py", "mypkg")

    def test_removes_empty_entry_from_list(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        settings: dict = {"hooks": {}}
        manager.add_hook_to_settings(settings, "SessionStart", "hook.py", "mypkg")
        assert len(settings["hooks"]["SessionStart"]) == 1

        # Act
        manager.remove_hook_from_settings(settings, "SessionStart", "hook.py", "mypkg")

        # Assert
        assert settings["hooks"]["SessionStart"] == []


class TestIsHookRegistered:
    def test_returns_false_when_event_absent(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        settings: dict = {"hooks": {}}

        # Act
        result = manager.is_hook_registered(settings, "SessionStart", "hook.py", "mypkg")

        # Assert
        assert result is False

    def test_returns_true_when_hook_present(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        settings: dict = {"hooks": {}}
        manager.add_hook_to_settings(settings, "SessionStart", "hook.py", "mypkg")

        # Act
        result = manager.is_hook_registered(settings, "SessionStart", "hook.py", "mypkg")

        # Assert
        assert result is True

    def test_with_matcher_matches_only_correct_matcher(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        settings: dict = {"hooks": {}}
        manager.add_hook_to_settings(
            settings, "PreToolUse", "hook.py", "mypkg", matcher="Edit|Write"
        )

        # Act
        correct = manager.is_hook_registered(
            settings, "PreToolUse", "hook.py", "mypkg", matcher="Edit|Write"
        )
        wrong = manager.is_hook_registered(
            settings, "PreToolUse", "hook.py", "mypkg", matcher="Read"
        )

        # Assert
        assert correct is True
        assert wrong is False

    def test_without_matcher_skips_matcher_entries(self, tmp_path: Path) -> None:
        # Arrange: hook registered only under a matcher entry
        manager = _make_manager(tmp_path)
        settings: dict = {"hooks": {}}
        manager.add_hook_to_settings(
            settings, "PreToolUse", "hook.py", "mypkg", matcher="Edit|Write"
        )

        # Act: check without matcher -> should not find the hook
        result = manager.is_hook_registered(settings, "PreToolUse", "hook.py", "mypkg")

        # Assert
        assert result is False


class TestLoadSaveSettings:
    def test_returns_default_when_file_does_not_exist(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)

        # Act
        settings = manager.load_settings(tmp_path)

        # Assert
        assert settings == {"hooks": {}}

    def test_roundtrip_correctly(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        data = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "foo"}]}]}}

        # Act
        manager.save_settings(tmp_path, data)
        loaded = manager.load_settings(tmp_path)

        # Assert
        assert loaded == data


class TestLoadSaveOrchestraJson:
    def test_returns_default_when_file_does_not_exist(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)

        # Act
        orch = manager.load_orchestra_json(tmp_path)

        # Assert
        assert "installed_packages" in orch
        assert orch["installed_packages"] == []

    def test_roundtrip_correctly(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        data = {
            "installed_packages": ["core"],
            "orchestra_dir": "/some/dir",
            "last_sync": "2026-01-01",
        }

        # Act
        manager.save_orchestra_json(tmp_path, data)
        loaded = manager.load_orchestra_json(tmp_path)

        # Assert
        assert loaded == data


class TestSetupEnvVar:
    """setup_env_var のテスト。"""

    @pytest.fixture(autouse=True)
    def _outside_venv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """既定では「venv の外で実行している」状態に固定する（Issue #343）。

        書き込み候補の選定は `sys.prefix != sys.base_prefix` を見るため、pytest 自身が venv
        内で走るかどうかで結果が変わってしまう。venv 側の分岐を検証するテストは、この
        fixture の後で `_pin_venv_interpreter` により自分で上書きする。
        """
        monkeypatch.setattr(hooks_mod.sys, "prefix", hooks_mod.sys.base_prefix)

    def test_dry_run_does_not_create_settings_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """dry-run 時は settings.json を作成しない。"""
        manager = _make_manager(tmp_path)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: tmp_path)

        manager.setup_env_var(dry_run=True)

        captured = capsys.readouterr()
        assert "[DRY-RUN] 環境変数設定" in captured.out
        assert not (tmp_path / ".claude" / "settings.json").exists()

    def test_writes_ai_orchestra_dir_into_global_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """settings.json に AI_ORCHESTRA_DIR を書き込む。"""
        manager = _make_manager(tmp_path)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: tmp_path)

        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps({"env": {"EXISTING": "1"}}), encoding="utf-8")

        manager.setup_env_var()

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert saved["env"]["EXISTING"] == "1"
        assert saved["env"]["AI_ORCHESTRA_DIR"] == str(tmp_path)
        assert saved["env"]["AI_ORCHESTRA_PYTHON"] == sys.executable

    def test_skips_when_already_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """同じ値が設定済みなら変更しない。"""
        manager = _make_manager(tmp_path)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: tmp_path)

        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(
                {
                    "env": {
                        "AI_ORCHESTRA_DIR": str(tmp_path),
                        "AI_ORCHESTRA_PYTHON": sys.executable,
                    }
                }
            ),
            encoding="utf-8",
        )
        before = settings_path.read_text(encoding="utf-8")

        manager.setup_env_var()

        captured = capsys.readouterr()
        assert "設定済み" in captured.out
        assert settings_path.read_text(encoding="utf-8") == before

    def test_writes_python_interpreter_when_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AI_ORCHESTRA_DIR 設定済みでも AI_ORCHESTRA_PYTHON は補完する（Issue #343）。

        既存導入プロジェクトの再 init で hook のインタプリタ固定が適用されるようにする。
        """
        manager = _make_manager(tmp_path)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: tmp_path)

        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({"env": {"AI_ORCHESTRA_DIR": str(tmp_path)}}),
            encoding="utf-8",
        )

        manager.setup_env_var()

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert saved["env"]["AI_ORCHESTRA_DIR"] == str(tmp_path)
        assert saved["env"]["AI_ORCHESTRA_PYTHON"] == sys.executable

    def test_preserves_user_specified_python_interpreter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """利用者が明示した AI_ORCHESTRA_PYTHON は上書きしない（逃げ道として維持）。"""
        manager = _make_manager(tmp_path)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: tmp_path)

        custom = tmp_path / "custom" / "bin" / "python3"
        custom.parent.mkdir(parents=True, exist_ok=True)
        custom.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        custom.chmod(0o755)
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({"env": {"AI_ORCHESTRA_PYTHON": str(custom)}}),
            encoding="utf-8",
        )

        manager.setup_env_var()

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert saved["env"]["AI_ORCHESTRA_PYTHON"] == str(custom)
        assert saved["env"]["AI_ORCHESTRA_DIR"] == str(tmp_path)

    def test_repairs_stale_python_interpreter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """解決できない AI_ORCHESTRA_PYTHON は再 init で修復する（Issue #343）。

        pipx/uvx の一時 venv やバージョン付きパスは容易に消える。凍結値が陳腐化すると
        全 hook が起動不能になり、同じ変数で起動する SessionStart 同期による自己修復も
        効かなくなるため、init/setup を復旧経路として機能させる。
        """
        manager = _make_manager(tmp_path)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: tmp_path)

        stale = str(tmp_path / "removed-venv" / "bin" / "python3")
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({"env": {"AI_ORCHESTRA_PYTHON": stale}}), encoding="utf-8"
        )

        manager.setup_env_var()

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        captured = capsys.readouterr()
        assert saved["env"]["AI_ORCHESTRA_PYTHON"] == sys.executable
        assert "hook を起動できません" in captured.err

    def test_repairs_non_executable_python_interpreter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """実在しても実行権のないパスは修復対象にする（起動できないため）。"""
        manager = _make_manager(tmp_path)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: tmp_path)

        not_executable = tmp_path / "python3"
        not_executable.write_text("", encoding="utf-8")
        not_executable.chmod(0o644)
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({"env": {"AI_ORCHESTRA_PYTHON": str(not_executable)}}), encoding="utf-8"
        )

        manager.setup_env_var()

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert saved["env"]["AI_ORCHESTRA_PYTHON"] == sys.executable

    def test_preserves_path_relative_python_interpreter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PATH で解決できる相対名の指定は尊重する（逃げ道の維持）。

        利用者が意図的に `python3` 等を指定する運用があるため、実在判定は
        Path.exists ではなく PATH 解決で行う。
        """
        manager = _make_manager(tmp_path)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: tmp_path)

        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        shim = shim_dir / "python3.99"
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", str(shim_dir))

        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({"env": {"AI_ORCHESTRA_PYTHON": "python3.99"}}), encoding="utf-8"
        )

        manager.setup_env_var()

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert saved["env"]["AI_ORCHESTRA_PYTHON"] == "python3.99"

    def test_repairs_python_interpreter_that_fails_version_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """起動できても requires-python を満たさないインタプリタは修復する（Issue #343）。

        本 Issue の実失敗は「PATH で解決はできるが hook を動かせない」インタプリタ
        （バージョンマネージャ未適用のログインシェルが拾う system python3 等）。
        PATH 解決だけを見る判定ではこのクラスを取りこぼす。
        """
        manager = _make_manager(tmp_path)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: tmp_path)

        outdated = tmp_path / "outdated-python3"
        outdated.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        outdated.chmod(0o755)
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({"env": {"AI_ORCHESTRA_PYTHON": str(outdated)}}), encoding="utf-8"
        )

        manager.setup_env_var()

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        captured = capsys.readouterr()
        assert saved["env"]["AI_ORCHESTRA_PYTHON"] == sys.executable
        assert "hook を起動できません" in captured.err

    def test_repairs_python_interpreter_without_pyyaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """pyyaml を import できないインタプリタは修復する（Issue #343）。

        `packages/codd/lib/codd_common.py` は top-level で `import yaml` しており、欠損時の
        ImportError は `hook_common.safe_hook_execution` が握り潰して exit 0 する。commit
        整合性ゲートが "Hook error" 1 行だけ残して黙って fail-open するため、requires-python
        を満たすだけでは hook を起動できたことにならない。
        """
        manager = _make_manager(tmp_path)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: tmp_path)

        # -S で site-packages を外すと、バージョンは満たすが yaml を import できなくなる。
        no_yaml = tmp_path / "no-yaml-python3"
        no_yaml.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n', encoding="utf-8")
        no_yaml.chmod(0o755)
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({"env": {"AI_ORCHESTRA_PYTHON": str(no_yaml)}}), encoding="utf-8"
        )

        manager.setup_env_var()

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        captured = capsys.readouterr()
        assert saved["env"]["AI_ORCHESTRA_PYTHON"] == sys.executable
        assert "pyyaml" in captured.err

    def test_does_not_pin_interpreter_inside_orchestra_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """worktree / プロジェクト venv の python は恒久設定へ書き込まない（Issue #343）。

        利用者グローバルの settings.json に焼き込むと、その venv を消した瞬間に全
        プロジェクトの hook が起動不能になり、同じ変数で起動する SessionStart 同期も
        道連れになって自己修復が効かなくなる。未設定なら PATH の python3 へ安全に劣化する。
        """
        repo_dir = tmp_path / "repo"
        home_dir = tmp_path / "home"
        manager = _make_manager(repo_dir)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: home_dir)

        venv_python = repo_dir / ".venv" / "bin" / "python3"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        venv_python.chmod(0o755)
        monkeypatch.setattr(hooks_mod.sys, "executable", str(venv_python))
        monkeypatch.setattr(hooks_mod.sys, "_base_executable", str(venv_python), raising=False)

        manager.setup_env_var()

        saved = json.loads((home_dir / ".claude" / "settings.json").read_text(encoding="utf-8"))
        captured = capsys.readouterr()
        assert hook_utils.HOOK_PYTHON_ENV_VAR not in saved["env"]
        assert saved["env"]["AI_ORCHESTRA_DIR"] == str(repo_dir)
        assert "設定しません" in captured.err

    def test_does_not_pin_venv_python_detached_from_orchestra_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AI_ORCHESTRA_DIR の外に作った venv の python は書き込まない（Issue #343）。

        editable install では AI_ORCHESTRA_DIR が安定した git repo を指す一方、venv だけ
        `~/.venvs/...` に置かれることがある。置き場所（orchestra_dir / tempdir）だけを見る
        ガードはこの形を素通しし、venv を消した瞬間に全プロジェクトの hook と SessionStart
        同期が同時に死ぬ。venv は起動プローブを通るため、プローブでも止められない。
        """
        repo_dir = tmp_path / "repo"
        home_dir = tmp_path / "home"
        manager = _make_manager(repo_dir)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: home_dir)
        monkeypatch.setattr(hooks_mod.tempfile, "gettempdir", lambda: str(tmp_path / "systmp"))

        venv_dir = tmp_path / "venvs" / "orchex-dev"
        venv_python = _write_launchable_python(venv_dir / "bin" / "python3")
        _pin_venv_interpreter(
            monkeypatch,
            executable=venv_python,
            prefix=venv_dir,
            base_executable=str(tmp_path / "gone" / "python3"),
        )

        manager.setup_env_var()

        saved = json.loads((home_dir / ".claude" / "settings.json").read_text(encoding="utf-8"))
        captured = capsys.readouterr()
        assert hook_utils.HOOK_PYTHON_ENV_VAR not in saved["env"]
        assert "設定しません" in captured.err

    def test_pins_venv_python_when_orchestra_dir_lives_inside_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """orchex 自身が venv の中にある構成では、その venv の python を書き込む。

        pip / pipx / uv tool 経由の導入では AI_ORCHESTRA_DIR も同じ venv 内を指すため、venv が
        消えればどちらにせよ hook は動かない。追加被害がないのに固定をやめると、pyyaml を持つ
        唯一のインタプリタを手放して `PATH` の `python3` へ落ちてしまう。
        """
        venv_dir = tmp_path / "pipx-venv"
        orchestra_dir = venv_dir / "lib" / "python3.12" / "site-packages" / "ai_orchestra"
        home_dir = tmp_path / "home"
        manager = _make_manager(orchestra_dir)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: home_dir)
        monkeypatch.setattr(hooks_mod.tempfile, "gettempdir", lambda: str(tmp_path / "systmp"))

        venv_python = _write_launchable_python(venv_dir / "bin" / "python3")
        _pin_venv_interpreter(
            monkeypatch,
            executable=venv_python,
            prefix=venv_dir,
            base_executable=str(tmp_path / "gone" / "python3"),
        )

        manager.setup_env_var()

        saved = json.loads((home_dir / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert saved["env"][hook_utils.HOOK_PYTHON_ENV_VAR] == str(venv_python)

    def test_falls_back_to_base_interpreter_for_detached_venv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非結合 venv では、起動できる基底インタプリタがあればそちらを書き込む。"""
        repo_dir = tmp_path / "repo"
        home_dir = tmp_path / "home"
        manager = _make_manager(repo_dir)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: home_dir)
        monkeypatch.setattr(hooks_mod.tempfile, "gettempdir", lambda: str(tmp_path / "systmp"))

        venv_dir = tmp_path / "venvs" / "orchex-dev"
        base_python = _write_launchable_python(tmp_path / "opt" / "python3")
        _pin_venv_interpreter(
            monkeypatch,
            executable=_write_launchable_python(venv_dir / "bin" / "python3"),
            prefix=venv_dir,
            base_executable=str(base_python),
        )

        manager.setup_env_var()

        saved = json.loads((home_dir / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert saved["env"][hook_utils.HOOK_PYTHON_ENV_VAR] == str(base_python)

    def test_does_not_pin_detached_venv_without_base_executable_attribute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`sys._base_executable` を持たないビルドでも例外にせず未設定にする。

        非公開属性のため存在を前提にできない。候補が尽きた場合は既存の「未設定 + 警告」経路へ
        落ちるだけで、新しい失敗経路は増やさない。
        """
        repo_dir = tmp_path / "repo"
        home_dir = tmp_path / "home"
        manager = _make_manager(repo_dir)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: home_dir)
        monkeypatch.setattr(hooks_mod.tempfile, "gettempdir", lambda: str(tmp_path / "systmp"))

        venv_dir = tmp_path / "venvs" / "orchex-dev"
        _pin_venv_interpreter(
            monkeypatch,
            executable=_write_launchable_python(venv_dir / "bin" / "python3"),
            prefix=venv_dir,
            base_executable=None,
        )

        manager.setup_env_var()

        saved = json.loads((home_dir / ".claude" / "settings.json").read_text(encoding="utf-8"))
        captured = capsys.readouterr()
        assert hook_utils.HOOK_PYTHON_ENV_VAR not in saved["env"]
        assert "設定しません" in captured.err

    def test_repairs_broken_value_even_with_ephemeral_interpreter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """既存値が起動できない場合は、消えうる場所の python でも機能回復を優先する。

        設定済みの値が死んでいる時点で全 hook が起動不能なので、死んだパスを残すより
        現在のインタプリタで動かせる状態に戻す方が良い。
        """
        repo_dir = tmp_path / "repo"
        home_dir = tmp_path / "home"
        manager = _make_manager(repo_dir)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: home_dir)

        venv_python = repo_dir / ".venv" / "bin" / "python3"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        venv_python.chmod(0o755)
        monkeypatch.setattr(hooks_mod.sys, "executable", str(venv_python))
        monkeypatch.setattr(hooks_mod.sys, "_base_executable", str(venv_python), raising=False)

        settings_path = home_dir / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(
            json.dumps({"env": {"AI_ORCHESTRA_PYTHON": str(tmp_path / "gone" / "python3")}}),
            encoding="utf-8",
        )

        manager.setup_env_var()

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        captured = capsys.readouterr()
        assert saved["env"]["AI_ORCHESTRA_PYTHON"] == str(venv_python)
        assert "hook を起動できません" in captured.err

    def test_min_hook_python_version_matches_requires_python(self) -> None:
        """プローブの下限は pyproject.toml の requires-python と一致させる。

        値を二重管理しているため、片方だけ上がったときにドリフトを検出する。
        """
        import tomllib

        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            requires_python = tomllib.load(f)["project"]["requires-python"]

        expected = ".".join(str(part) for part in hooks_mod.MIN_HOOK_PYTHON_VERSION)
        assert requires_python == f">={expected}"

    def test_keeps_main_path_when_running_from_linked_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """linked worktree からの実行では既存の main パスを保持する。"""
        main_dir = tmp_path / "main"
        worktree_dir = tmp_path / "feature"
        home_dir = tmp_path / "home"
        main_dir.mkdir()
        manager = _make_manager(worktree_dir)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: home_dir)
        monkeypatch.setattr(
            hooks_mod,
            "_is_linked_worktree_of",
            lambda candidate, existing: (candidate, existing) == (worktree_dir, main_dir),
            raising=False,
        )

        settings_path = home_dir / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(
            json.dumps({"env": {"AI_ORCHESTRA_DIR": str(main_dir)}}),
            encoding="utf-8",
        )

        manager.setup_env_var()

        captured = capsys.readouterr()
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "linked worktree" in captured.err
        assert str(main_dir) in captured.err
        assert saved["env"]["AI_ORCHESTRA_DIR"] == str(main_dir)

    def test_sets_python_interpreter_even_from_linked_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """worktree 実行でも AI_ORCHESTRA_PYTHON は補完する（Issue #343）。

        AI_ORCHESTRA_DIR の保持と hook 起動用インタプリタの固定は独立した関心事であり、
        worktree 前提の開発フローで修正が一切届かなくなるのを防ぐ。
        """
        main_dir = tmp_path / "main"
        worktree_dir = tmp_path / "feature"
        home_dir = tmp_path / "home"
        main_dir.mkdir()
        manager = _make_manager(worktree_dir)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: home_dir)
        monkeypatch.setattr(
            hooks_mod,
            "_is_linked_worktree_of",
            lambda candidate, existing: (candidate, existing) == (worktree_dir, main_dir),
            raising=False,
        )

        settings_path = home_dir / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(
            json.dumps({"env": {"AI_ORCHESTRA_DIR": str(main_dir)}}),
            encoding="utf-8",
        )

        manager.setup_env_var()

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert saved["env"]["AI_ORCHESTRA_DIR"] == str(main_dir)
        assert saved["env"]["AI_ORCHESTRA_PYTHON"] == sys.executable

    def test_replaces_existing_path_when_current_dir_is_not_its_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """別リポジトリへの切り替えは従来どおり許可する。"""
        existing_dir = tmp_path / "existing"
        current_dir = tmp_path / "current"
        home_dir = tmp_path / "home"
        manager = _make_manager(current_dir)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: home_dir)
        monkeypatch.setattr(
            hooks_mod,
            "_is_linked_worktree_of",
            lambda _candidate, _existing: False,
        )

        settings_path = home_dir / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(
            json.dumps({"env": {"AI_ORCHESTRA_DIR": str(existing_dir)}}),
            encoding="utf-8",
        )

        manager.setup_env_var()

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert saved["env"]["AI_ORCHESTRA_DIR"] == str(current_dir)

    def test_detects_linked_worktree_from_git_metadata(self, tmp_path: Path) -> None:
        """配置名に依存せず、Git metadata から main と linked worktree を判定する。"""
        main_dir = tmp_path / "main"
        worktree_dir = tmp_path / "arbitrary-location"
        hooks_dir = tmp_path / "empty-hooks"
        main_dir.mkdir()
        hooks_dir.mkdir()
        subprocess.run(
            ["git", "init", "--template="], cwd=main_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "core.hooksPath", str(hooks_dir)],
            cwd=main_dir,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=main_dir,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=main_dir,
            check=True,
        )
        (main_dir / "README.md").write_text("test\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=main_dir, check=True)
        subprocess.run(
            ["git", "commit", "--no-verify", "--no-gpg-sign", "-m", "initial"],
            cwd=main_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "worktree", "add", "-b", "test-worktree", str(worktree_dir)],
            cwd=main_dir,
            check=True,
            capture_output=True,
        )

        assert hooks_mod._is_linked_worktree_of(worktree_dir, main_dir) is True
        assert hooks_mod._is_linked_worktree_of(main_dir, worktree_dir) is False


class TestSyncHookOperations:
    """sync hook 関連メソッドのテスト。"""

    def test_is_sync_hook_registered_ignores_matcher_entries(self, tmp_path: Path) -> None:
        """matcher 付きエントリだけでは登録済みとみなさない。"""
        manager = _make_manager(tmp_path)
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "Task",
                        "hooks": [{"type": "command", "command": manager.SYNC_HOOK_COMMAND}],
                    }
                ]
            }
        }

        assert manager.is_sync_hook_registered(settings) is False

    def test_is_sync_hook_registered_returns_true_for_plain_session_start(
        self, tmp_path: Path
    ) -> None:
        """matcher なしの SessionStart hook を検出する。"""
        manager = _make_manager(tmp_path)
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [{"type": "command", "command": manager.SYNC_HOOK_COMMAND}],
                    }
                ]
            }
        }

        assert manager.is_sync_hook_registered(settings) is True

    def test_is_sync_hook_registered_detects_legacy_interpreter_form(self, tmp_path: Path) -> None:
        """旧表記（リテラル python3）の登録も検出する（重複登録の防止、Issue #343）。"""
        manager = _make_manager(tmp_path)
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'python3 "$AI_ORCHESTRA_DIR/scripts/sync-orchestra.py"',
                            }
                        ],
                    }
                ]
            }
        }

        assert manager.is_sync_hook_registered(settings) is True

    def test_remove_sync_hook_removes_legacy_interpreter_form(self, tmp_path: Path) -> None:
        """旧表記の sync hook も削除対象にする（取り残しの防止、Issue #343）。"""
        manager = _make_manager(tmp_path)
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'python3 "$AI_ORCHESTRA_DIR/scripts/sync-orchestra.py"',
                            }
                        ]
                    }
                ]
            }
        }

        manager.remove_sync_hook(settings)

        assert settings["hooks"]["SessionStart"] == []

    def test_register_sync_hook_creates_entry(self, tmp_path: Path) -> None:
        """sync hook を新規登録する。"""
        manager = _make_manager(tmp_path)
        settings: dict = {}

        manager.register_sync_hook(settings)

        hooks = settings["hooks"]["SessionStart"][0]["hooks"]
        assert hooks == [
            {
                "type": "command",
                "command": manager.SYNC_HOOK_COMMAND,
                "timeout": manager.SYNC_HOOK_TIMEOUT,
            }
        ]

    def test_register_sync_hook_is_idempotent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """既存登録がある場合は重複追加しない。"""
        manager = _make_manager(tmp_path)
        settings = {"hooks": {"SessionStart": [{"hooks": []}]}}
        manager.register_sync_hook(settings)

        manager.register_sync_hook(settings)

        captured = capsys.readouterr()
        hooks = settings["hooks"]["SessionStart"][0]["hooks"]
        assert len(hooks) == 1
        assert "登録済み" in captured.out

    def test_register_sync_hook_dry_run_does_not_mutate(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """dry-run 時は settings を変更しない。"""
        manager = _make_manager(tmp_path)
        settings: dict = {}

        manager.register_sync_hook(settings, dry_run=True)

        captured = capsys.readouterr()
        assert "[DRY-RUN]" in captured.out
        assert settings == {}

    def test_remove_sync_hook_removes_only_target_command(self, tmp_path: Path) -> None:
        """sync hook だけを削除し、他の hook は残す。"""
        manager = _make_manager(tmp_path)
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": manager.SYNC_HOOK_COMMAND},
                            {"type": "command", "command": "python3 other.py"},
                        ]
                    }
                ]
            }
        }

        manager.remove_sync_hook(settings)

        assert settings["hooks"]["SessionStart"] == [
            {"hooks": [{"type": "command", "command": "python3 other.py"}]}
        ]

    def test_remove_sync_hook_keeps_matcher_entries(self, tmp_path: Path) -> None:
        """matcher 付きエントリはそのまま残す。"""
        manager = _make_manager(tmp_path)
        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": manager.SYNC_HOOK_COMMAND}]},
                    {
                        "matcher": "Task",
                        "hooks": [{"type": "command", "command": "python3 keep.py"}],
                    },
                ]
            }
        }

        manager.remove_sync_hook(settings)

        assert settings["hooks"]["SessionStart"] == [
            {"matcher": "Task", "hooks": [{"type": "command", "command": "python3 keep.py"}]}
        ]


class TestEphemeralInterpreterDetection:
    """恒久設定へ焼き込めないインタプリタの判定（Issue #343）。"""

    def test_detects_venv_symlink_by_unresolved_path(self, tmp_path: Path) -> None:
        """venv の bin/python は symlink 先ではなく未解決パスで判定する。

        `bin/python` は基底インタプリタへの symlink なので realpath は venv の外を指す。
        しかし設定へ焼き込まれて venv 削除で死ぬのは未解決パスの方であり、realpath だけを
        見るガードは本来弾くべき値をすべて素通しする。
        """
        repo_dir = tmp_path / "repo"
        venv_python = repo_dir / ".venv" / "bin" / "python3"
        venv_python.parent.mkdir(parents=True)
        venv_python.symlink_to(sys.executable)

        assert not _is_within(venv_python.resolve(), repo_dir)
        assert hooks_mod._is_ephemeral_interpreter(str(venv_python), [repo_dir]) is True

    def test_accepts_interpreter_outside_roots(self, tmp_path: Path) -> None:
        """`_is_ephemeral_interpreter` は与えられた root 配下かどうかだけを答える。

        「その venv が消えても AI_ORCHESTRA_DIR は生き残るか」の判断はこの関数ではなく root の
        構成側（`_ephemeral_interpreter_roots` / `_detached_venv_root`）が持つ。責務の置き場所
        を固定するため、ここでは root 外なら False であることだけを確認する。
        """
        assert hooks_mod._is_ephemeral_interpreter(sys.executable, [tmp_path / "repo"]) is False

    def test_roots_include_persisted_orchestra_dir(self, tmp_path: Path) -> None:
        """記録済みの AI_ORCHESTRA_DIR も対象ツリーに含める。

        worktree から実行しつつ venv は main リポジトリ側、というケースを取りこぼさない。
        """
        manager = _make_manager(tmp_path / "worktree")
        main_dir = tmp_path / "main"

        roots = manager._ephemeral_interpreter_roots({"AI_ORCHESTRA_DIR": str(main_dir)})

        assert main_dir in roots
        assert tmp_path / "worktree" in roots

    def test_roots_include_detached_venv_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AI_ORCHESTRA_DIR の外にある venv の prefix も対象ツリーに含める。

        editable install で venv だけ別の場所に置く構成では、venv が消えても
        AI_ORCHESTRA_DIR は残る。この非結合ケースこそ恒久設定へ焼き込んではいけない。
        """
        venv_dir = tmp_path / "venvs" / "orchex-dev"
        monkeypatch.setattr(hooks_mod.sys, "prefix", str(venv_dir))
        monkeypatch.setattr(hooks_mod.sys, "base_prefix", str(tmp_path / "opt" / "python"))
        manager = _make_manager(tmp_path / "repo")

        roots = manager._ephemeral_interpreter_roots({})

        assert venv_dir in roots

    def test_roots_exclude_venv_that_contains_orchestra_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """orchex 自身が入っている venv は対象にしない（pip / pipx / uv tool 経由）。

        AI_ORCHESTRA_DIR が同じ venv の中にあるなら、venv 消滅時にはどちらにせよ hook は
        動かない。追加被害がないのに固定をやめると `PATH` の `python3` へ落ちてしまう。
        """
        venv_dir = tmp_path / "pipx-venv"
        orchestra_dir = venv_dir / "lib" / "python3.12" / "site-packages" / "ai_orchestra"
        monkeypatch.setattr(hooks_mod.sys, "prefix", str(venv_dir))
        monkeypatch.setattr(hooks_mod.sys, "base_prefix", str(tmp_path / "opt" / "python"))
        manager = _make_manager(orchestra_dir)

        roots = manager._ephemeral_interpreter_roots({})

        assert venv_dir not in roots

    def test_roots_exclude_venv_when_persisted_orchestra_dir_lives_inside(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """結合判定は記録済みの AI_ORCHESTRA_DIR にも効かせる。"""
        venv_dir = tmp_path / "pipx-venv"
        persisted_dir = venv_dir / "lib" / "python3.12" / "site-packages" / "ai_orchestra"
        monkeypatch.setattr(hooks_mod.sys, "prefix", str(venv_dir))
        monkeypatch.setattr(hooks_mod.sys, "base_prefix", str(tmp_path / "opt" / "python"))
        manager = _make_manager(tmp_path / "repo")

        roots = manager._ephemeral_interpreter_roots({"AI_ORCHESTRA_DIR": str(persisted_dir)})

        assert venv_dir not in roots


def _is_within(path: Path, root: Path) -> bool:
    """テスト用: path が root 配下か判定する。"""
    return path == root or root in path.parents
