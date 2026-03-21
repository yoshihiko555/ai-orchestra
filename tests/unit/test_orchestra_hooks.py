"""orchestra_hooks.py の HooksMixin テスト。

OrchestraManager 経由で HooksMixin のメソッドをテストする。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.module_loader import load_module

manager_mod = load_module("orchestra_manager", "scripts/orchestra-manager.py")
OrchestraManager = manager_mod.OrchestraManager
models_mod = load_module("orchestra_models", "scripts/orchestra_models.py")
HookEntry = models_mod.HookEntry
Package = models_mod.Package


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
