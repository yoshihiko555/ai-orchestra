"""orchestra_models.py の HookEntry / ScriptEntry / Package テスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.module_loader import load_module

models_mod = load_module("orchestra_models", "scripts/lib/orchestra_models.py")
HookEntry = models_mod.HookEntry
ScriptEntry = models_mod.ScriptEntry
Package = models_mod.Package


class TestHookEntryFromJson:
    def test_string_value_sets_defaults(self) -> None:
        # Arrange / Act
        entry = HookEntry.from_json("hook.py")

        # Assert
        assert entry.file == "hook.py"
        assert entry.matcher is None
        assert entry.timeout == 5

    def test_dict_value_parses_all_fields(self) -> None:
        # Arrange
        value = {"file": "h.py", "matcher": "*.py", "timeout": 10}

        # Act
        entry = HookEntry.from_json(value)

        # Assert
        assert entry.file == "h.py"
        assert entry.matcher == "*.py"
        assert entry.timeout == 10

    def test_dict_without_timeout_uses_default(self) -> None:
        # Arrange
        value = {"file": "h.py"}

        # Act
        entry = HookEntry.from_json(value)

        # Assert
        assert entry.timeout == 5


class TestScriptEntryFromJson:
    def test_string_value_sets_path_only(self) -> None:
        # Arrange / Act
        entry = ScriptEntry.from_json("scripts/dashboard.py")

        # Assert
        assert entry.path == "scripts/dashboard.py"
        assert entry.description == ""

    def test_dict_value_parses_all_fields(self) -> None:
        # Arrange
        value = {"path": "scripts/dashboard.py", "description": "テキストダッシュボード"}

        # Act
        entry = ScriptEntry.from_json(value)

        # Assert
        assert entry.path == "scripts/dashboard.py"
        assert entry.description == "テキストダッシュボード"

    def test_dict_without_description_uses_default(self) -> None:
        # Arrange
        value = {"path": "scripts/kpi-report.py"}

        # Act
        entry = ScriptEntry.from_json(value)

        # Assert
        assert entry.path == "scripts/kpi-report.py"
        assert entry.description == ""

    def test_dict_missing_path_raises_value_error(self) -> None:
        # Arrange
        value = {"description": "no path key"}

        # Act / Assert
        with pytest.raises(ValueError, match="requires 'path' key"):
            ScriptEntry.from_json(value)


class TestPackageLoad:
    def test_parses_minimal_manifest(self, tmp_path: Path) -> None:
        # Arrange
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps({"name": "mypkg", "version": "1.2.3"}), encoding="utf-8"
        )

        # Act
        pkg = Package.load(manifest_path)

        # Assert
        assert pkg.name == "mypkg"
        assert pkg.version == "1.2.3"

    def test_parses_hooks_as_hook_entry_instances(self, tmp_path: Path) -> None:
        # Arrange
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "name": "mypkg",
                    "version": "1.0.0",
                    "hooks": {
                        "SessionStart": ["hook_a.py"],
                        "PreToolUse": [{"file": "hook_b.py", "matcher": "Edit|Write"}],
                    },
                }
            ),
            encoding="utf-8",
        )

        # Act
        pkg = Package.load(manifest_path)

        # Assert
        assert isinstance(pkg.hooks["SessionStart"][0], HookEntry)
        assert isinstance(pkg.hooks["PreToolUse"][0], HookEntry)
        assert pkg.hooks["SessionStart"][0].file == "hook_a.py"
        assert pkg.hooks["PreToolUse"][0].matcher == "Edit|Write"

    def test_sets_path_to_manifest_parent(self, tmp_path: Path) -> None:
        # Arrange
        pkg_dir = tmp_path / "packages" / "mypkg"
        pkg_dir.mkdir(parents=True)
        manifest_path = pkg_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps({"name": "mypkg", "version": "1.0.0"}), encoding="utf-8"
        )

        # Act
        pkg = Package.load(manifest_path)

        # Assert
        assert pkg.path == pkg_dir

    def test_parses_scripts_as_script_entry_instances(self, tmp_path: Path) -> None:
        # Arrange
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "name": "mypkg",
                    "version": "1.0.0",
                    "scripts": [
                        "scripts/simple.py",
                        {
                            "path": "scripts/dashboard.py",
                            "description": "ダッシュボード",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        # Act
        pkg = Package.load(manifest_path)

        # Assert
        assert len(pkg.scripts) == 2
        assert isinstance(pkg.scripts[0], ScriptEntry)
        assert isinstance(pkg.scripts[1], ScriptEntry)
        assert pkg.scripts[0].path == "scripts/simple.py"
        assert pkg.scripts[0].description == ""
        assert pkg.scripts[1].path == "scripts/dashboard.py"
        assert pkg.scripts[1].description == "ダッシュボード"

    def test_empty_lists_default_correctly(self, tmp_path: Path) -> None:
        # Arrange
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps({"name": "mypkg", "version": "1.0.0"}), encoding="utf-8"
        )

        # Act
        pkg = Package.load(manifest_path)

        # Assert
        assert pkg.depends == []
        assert pkg.files == []
        assert pkg.scripts == []
        assert pkg.config == []
        assert pkg.skills == []
        assert pkg.agents == []
        assert pkg.rules == []
        assert pkg.hooks == {}
