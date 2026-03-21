"""orchestra_models.py の HookEntry / Package テスト。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.module_loader import load_module

models_mod = load_module("orchestra_models", "scripts/orchestra_models.py")
HookEntry = models_mod.HookEntry
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
