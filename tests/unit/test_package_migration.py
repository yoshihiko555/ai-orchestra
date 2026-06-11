"""sync-orchestra.py の installed_packages 移行（パッケージリネーム読み替え）のテスト。"""

from __future__ import annotations

from tests.module_loader import load_module

sync_orchestra = load_module("sync_orchestra", "scripts/sync-orchestra.py")
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
