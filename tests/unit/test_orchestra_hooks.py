"""orchestra_hooks.py の HooksMixin テスト。

OrchestraManager 経由で HooksMixin のメソッドをテストする。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.module_loader import load_module

manager_mod = load_module("orchestra_manager", "scripts/orchestra-manager.py")
OrchestraManager = manager_mod.OrchestraManager
models_mod = load_module("orchestra_models", "scripts/lib/orchestra_models.py")
HookEntry = models_mod.HookEntry
Package = models_mod.Package
hooks_mod = sys.modules[manager_mod.HooksMixin.__module__]


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

    def test_skips_when_already_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """同じ値が設定済みなら変更しない。"""
        manager = _make_manager(tmp_path)
        monkeypatch.setattr(hooks_mod.Path, "home", lambda: tmp_path)

        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({"env": {"AI_ORCHESTRA_DIR": str(tmp_path)}}),
            encoding="utf-8",
        )
        before = settings_path.read_text(encoding="utf-8")

        manager.setup_env_var()

        captured = capsys.readouterr()
        assert "設定済み" in captured.out
        assert settings_path.read_text(encoding="utf-8") == before


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
