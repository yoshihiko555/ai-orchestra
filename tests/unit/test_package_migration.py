"""sync-orchestra.py の installed_packages 移行（リネーム読み替え・配布終了パッケージ除去）のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.module_loader import load_module

sync_orchestra = load_module("sync_orchestra", "scripts/sync-orchestra.py")
hook_utils = load_module("hook_utils_for_package_migration_test", "scripts/lib/hook_utils.py")
migrate_installed_packages = sync_orchestra.migrate_installed_packages


class TestMigrateInstalledPackages:
    """migrate_installed_packages のテスト。"""

    def test_renames_gemini_suggestions(self) -> None:
        """旧 gemini-suggestions は antigravity-suggestions に読み替えられる。"""
        migrated, changed = migrate_installed_packages(["core", "gemini-suggestions"])
        assert migrated == ["core", "antigravity-suggestions"]
        assert changed is True

    def test_no_change_for_current_names(self) -> None:
        """新名のみの場合は変更なし。"""
        packages = ["core", "antigravity-suggestions", "audit"]
        migrated, changed = migrate_installed_packages(packages)
        assert migrated == packages
        assert changed is False

    def test_dedupes_when_both_names_present(self) -> None:
        """旧名と新名が両方ある場合は重複を除去する。"""
        migrated, changed = migrate_installed_packages(
            ["gemini-suggestions", "antigravity-suggestions"]
        )
        assert migrated == ["antigravity-suggestions"]
        assert changed is True

    def test_preserves_order(self) -> None:
        """元の順序を保つ。"""
        migrated, _ = migrate_installed_packages(["audit", "gemini-suggestions", "core"])
        assert migrated == ["audit", "antigravity-suggestions", "core"]

    def test_empty_list(self) -> None:
        """空リストはそのまま。"""
        migrated, changed = migrate_installed_packages([])
        assert migrated == []
        assert changed is False

    def test_removes_discontinued_package(self) -> None:
        """配布を終了した cocoindex は installed_packages から除去される。"""
        migrated, changed = migrate_installed_packages(["core", "cocoindex", "audit"])
        assert migrated == ["core", "audit"]
        assert changed is True


class TestDropRemovedPackageHashes:
    """drop_removed_package_hashes のテスト。"""

    def test_drops_only_discontinued_package_entries(self) -> None:
        """配布終了パッケージの台帳だけを消し、他パッケージの台帳は残す。"""
        orch = {
            "file_hashes": {
                "core": {"config/core/task-memory.yaml": "abc"},
                "cocoindex": {"config/cocoindex/cocoindex.yaml": "def"},
            }
        }
        dropped = sync_orchestra.drop_removed_package_hashes(orch)
        assert dropped is True
        assert orch["file_hashes"] == {"core": {"config/core/task-memory.yaml": "abc"}}

    def test_no_discontinued_entries_reports_no_change(self) -> None:
        """配布終了パッケージの台帳が無ければ変更なしを返す（不要な保存を起こさない）。"""
        orch = {"file_hashes": {"core": {"config/core/task-memory.yaml": "abc"}}}
        assert sync_orchestra.drop_removed_package_hashes(orch) is False

    def test_missing_file_hashes_is_noop(self) -> None:
        """file_hashes が無い orchestra.json でも例外にならない。"""
        orch: dict = {"installed_packages": ["core"]}
        assert sync_orchestra.drop_removed_package_hashes(orch) is False
        assert orch == {"installed_packages": ["core"]}


class TestMainWithOnlyDiscontinuedPackage:
    """配布終了パッケージしか導入されていないプロジェクトの SessionStart 同期。"""

    def test_removal_is_saved_and_hooks_are_detached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """installed_packages が空になっても、除去の保存と hook の取り外しは行われる。

        除去後に導入パッケージが空になる経路は早期 return するため、ここで保存と
        hook の取り外しをしないと cocoindex の名前と hook が残り続ける。
        """
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        project_dir = tmp_path / "project"
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        orch_path = claude_dir / "orchestra.json"
        orch_path.write_text(
            json.dumps(
                {
                    "installed_packages": ["cocoindex"],
                    "file_hashes": {"cocoindex": {"config/cocoindex/cocoindex.yaml": "abc"}},
                }
            ),
            encoding="utf-8",
        )
        removed_command = hook_utils.get_hook_command("cocoindex", "notify-proxy-reconnect.py")
        settings_path = claude_dir / "settings.local.json"
        settings_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {"type": "command", "command": hook_utils.SYNC_HOOK_COMMAND}
                                ]
                            }
                        ],
                        "UserPromptSubmit": [
                            {"hooks": [{"type": "command", "command": removed_command}]}
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(orchestra_dir))
        monkeypatch.setattr(sync_orchestra, "read_hook_input", lambda: {"cwd": str(project_dir)})

        sync_orchestra.main()

        orch = json.loads(orch_path.read_text(encoding="utf-8"))
        assert orch["installed_packages"] == []
        assert "cocoindex" not in orch["file_hashes"]
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        registered = [
            hook["command"]
            for entries in settings["hooks"].values()
            for entry in entries
            for hook in entry["hooks"]
        ]
        assert registered == [hook_utils.SYNC_HOOK_COMMAND]
