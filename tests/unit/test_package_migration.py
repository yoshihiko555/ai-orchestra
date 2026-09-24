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


class TestKeepChangedRemovedPackageFiles:
    """keep_changed_removed_package_files のテスト（uninstall と同じ保護方針）。"""

    CONFIG_KEY = "config/cocoindex/cocoindex.yaml"

    def _write_config(self, claude_dir: Path, content: str) -> Path:
        target = claude_dir / self.CONFIG_KEY
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def test_unchanged_file_is_left_for_stale_removal(self, tmp_path: Path) -> None:
        """配布時ハッシュと一致するファイルは synced_files に残し、stale 削除に任せる。"""
        target = self._write_config(tmp_path, "enabled: true\n")
        orch = {
            "synced_files": [self.CONFIG_KEY],
            "file_hashes": {
                "cocoindex": {self.CONFIG_KEY: sync_orchestra.compute_file_hash(target)}
            },
        }
        assert sync_orchestra.keep_changed_removed_package_files(tmp_path, orch) == []
        assert orch["synced_files"] == [self.CONFIG_KEY]

    def test_modified_file_is_kept(self, tmp_path: Path) -> None:
        """配布後に編集されたファイルは synced_files から外し、削除させない。"""
        self._write_config(tmp_path, "enabled: false  # edited\n")
        orch = {
            "synced_files": ["agents/planner.md", self.CONFIG_KEY],
            "file_hashes": {"cocoindex": {self.CONFIG_KEY: "hash-at-distribution"}},
        }
        kept = sync_orchestra.keep_changed_removed_package_files(tmp_path, orch)
        assert kept == [self.CONFIG_KEY]
        assert orch["synced_files"] == ["agents/planner.md"]

    def test_file_without_recorded_hash_is_kept(self, tmp_path: Path) -> None:
        """ハッシュ未記録のファイルは変更有無を判定できないため残す。"""
        self._write_config(tmp_path, "enabled: true\n")
        orch = {"synced_files": [self.CONFIG_KEY], "file_hashes": {}}
        assert sync_orchestra.keep_changed_removed_package_files(tmp_path, orch) == [
            self.CONFIG_KEY
        ]


class TestMainWithOnlyDiscontinuedPackage:
    """配布終了パッケージしか導入されていないプロジェクトの SessionStart 同期。

    `orchex install cocoindex` は依存先の core が無くても続行するため、この状態は実在しうる。
    除去後に導入一覧が空になっても、保存・hook の取り外し・stale 配布物の掃除が行われなければ
    ならない。
    """

    CONFIG_KEY = "config/cocoindex/cocoindex.yaml"

    def _setup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, edit_config: bool
    ) -> tuple[Path, Path, Path]:
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        project_dir = tmp_path / "project"
        claude_dir = project_dir / ".claude"
        config_path = claude_dir / self.CONFIG_KEY
        config_path.parent.mkdir(parents=True)
        config_path.write_text("enabled: true\n", encoding="utf-8")
        recorded_hash = sync_orchestra.compute_file_hash(config_path)
        if edit_config:
            config_path.write_text("enabled: false  # edited\n", encoding="utf-8")
        orch_path = claude_dir / "orchestra.json"
        orch_path.write_text(
            json.dumps(
                {
                    "installed_packages": ["cocoindex"],
                    "synced_files": [self.CONFIG_KEY],
                    "file_hashes": {"cocoindex": {self.CONFIG_KEY: recorded_hash}},
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
        return orch_path, settings_path, config_path

    def test_removal_is_saved_hooks_detached_and_stale_config_deleted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未変更の同期済み config は削除され、導入一覧・台帳・hook からも外れる。"""
        orch_path, settings_path, config_path = self._setup(
            tmp_path, monkeypatch, edit_config=False
        )

        sync_orchestra.main()

        orch = json.loads(orch_path.read_text(encoding="utf-8"))
        assert orch["installed_packages"] == []
        assert "cocoindex" not in orch["file_hashes"]
        assert orch["synced_files"] == []
        assert not config_path.exists()
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        registered = [
            hook["command"]
            for entries in settings["hooks"].values()
            for entry in entries
            for hook in entry["hooks"]
        ]
        assert registered == [hook_utils.SYNC_HOOK_COMMAND]

    def test_edited_config_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """配布後に編集された config は削除せず残し、その旨を出力する。"""
        orch_path, _settings_path, config_path = self._setup(
            tmp_path, monkeypatch, edit_config=True
        )

        sync_orchestra.main()

        assert config_path.read_text(encoding="utf-8") == "enabled: false  # edited\n"
        orch = json.loads(orch_path.read_text(encoding="utf-8"))
        assert orch["installed_packages"] == []
        assert self.CONFIG_KEY not in orch["synced_files"]
        assert self.CONFIG_KEY in capsys.readouterr().out
