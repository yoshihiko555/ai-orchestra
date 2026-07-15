"""Issue #236: orchex CLI 評価セット突合で確認された実装ギャップの回帰テスト。

対象観点（docs/evaluation/orchex-cli.md）:
- EV-28（境界 / must）: `uninstall --dry-run` は最後のパッケージ削除時でも
  settings.local.json を実書き換えしない。
- EV-13（正常 / must, 関連）: `enable` は `installed_packages` に無いパッケージへ
  実行してもフックを登録しない。
- EV-08（異常 / must, 追加ギャップ）: `install()` が `_copy_config_if_safe` で
  ハッシュ保護した config ファイルを、直後の `run_initial_sync()` が
  mtime のみの `needs_sync()` で再度上書きしない。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from tests.module_loader import load_module

manager_mod = load_module("orchestra_manager_gaps", "scripts/orchestra-manager.py")
OrchestraManager = manager_mod.OrchestraManager


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_manifest(packages_dir: Path, name: str, **extra) -> Path:
    pkg_dir = packages_dir / name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "name": name,
        "version": "1.0.0",
        "description": "",
        "depends": [],
        "hooks": {"SessionStart": [{"file": "hook.py"}]},
        "config": [],
        "agents": [],
    }
    manifest.update(extra)
    manifest_path = pkg_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


class TestUninstallDryRunDoesNotTouchSettings:
    def test_last_package_dry_run_does_not_write_settings(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        _write_manifest(orchestra_dir / "packages", "mypkg")

        project_dir = tmp_path / "project"
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "orchestra.json").write_text(
            json.dumps({"installed_packages": ["mypkg"], "file_hashes": {}}),
            encoding="utf-8",
        )
        settings_path = claude_dir / "settings.local.json"
        original_settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "command": OrchestraManager.SYNC_HOOK_COMMAND,
                                "timeout": OrchestraManager.SYNC_HOOK_TIMEOUT,
                            }
                        ]
                    }
                ]
            }
        }
        settings_path.write_text(json.dumps(original_settings), encoding="utf-8")
        settings_mtime_before = settings_path.stat().st_mtime

        manager = OrchestraManager(orchestra_dir)
        manager.uninstall("mypkg", str(project_dir), dry_run=True)

        # settings.local.json はバイト単位で不変（書き込まれていない）
        assert settings_path.stat().st_mtime == settings_mtime_before
        assert json.loads(settings_path.read_text()) == original_settings

        # orchestra.json も dry-run では書き換わらない
        orch_after = json.loads((claude_dir / "orchestra.json").read_text())
        assert orch_after["installed_packages"] == ["mypkg"]

    def test_last_package_real_run_removes_sync_hook(self, tmp_path: Path) -> None:
        """対照テスト: dry_run=False では従来どおり sync hook が解除される。"""
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        _write_manifest(orchestra_dir / "packages", "mypkg")

        project_dir = tmp_path / "project"
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "orchestra.json").write_text(
            json.dumps({"installed_packages": ["mypkg"], "file_hashes": {}}),
            encoding="utf-8",
        )
        settings_path = claude_dir / "settings.local.json"
        settings_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "command": OrchestraManager.SYNC_HOOK_COMMAND,
                                        "timeout": OrchestraManager.SYNC_HOOK_TIMEOUT,
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        manager = OrchestraManager(orchestra_dir)
        manager.uninstall("mypkg", str(project_dir), dry_run=False)

        settings_after = json.loads(settings_path.read_text())
        assert settings_after.get("hooks", {}).get("SessionStart", []) == []
        orch_after = json.loads((claude_dir / "orchestra.json").read_text())
        assert orch_after["installed_packages"] == []


class TestEnableRequiresInstalled:
    def test_enable_uninstalled_package_exits_with_error_and_does_not_register_hook(
        self, tmp_path: Path, capsys
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        _write_manifest(orchestra_dir / "packages", "mypkg")

        project_dir = tmp_path / "project"
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "orchestra.json").write_text(
            json.dumps({"installed_packages": [], "file_hashes": {}}), encoding="utf-8"
        )

        manager = OrchestraManager(orchestra_dir)

        import pytest

        with pytest.raises(SystemExit) as exc_info:
            manager.enable("mypkg", str(project_dir), dry_run=False)

        assert exc_info.value.code == 1
        assert "インストールされていません" in capsys.readouterr().err

        settings_path = claude_dir / "settings.local.json"
        assert not settings_path.exists()

    def test_enable_installed_package_registers_hook(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        _write_manifest(orchestra_dir / "packages", "mypkg")

        project_dir = tmp_path / "project"
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "orchestra.json").write_text(
            json.dumps({"installed_packages": ["mypkg"], "file_hashes": {}}), encoding="utf-8"
        )

        manager = OrchestraManager(orchestra_dir)
        manager.enable("mypkg", str(project_dir), dry_run=False)

        settings_after = json.loads((claude_dir / "settings.local.json").read_text())
        assert settings_after["hooks"]["SessionStart"], "フックが登録されているはず"


class TestRunInitialSyncPreservesUserModifiedConfig:
    def test_hash_mismatch_skips_overwrite_even_when_source_is_newer(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        pkg_dir = orchestra_dir / "packages" / "mypkg"
        (pkg_dir / "config").mkdir(parents=True)
        (pkg_dir / "config" / "foo.yaml").write_text("distributed: v2", encoding="utf-8")
        _write_manifest(orchestra_dir / "packages", "mypkg", config=["config/foo.yaml"])
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(orchestra_dir))

        project_dir = tmp_path / "project"
        claude_dir = project_dir / ".claude"
        dst = claude_dir / "config" / "mypkg" / "foo.yaml"
        dst.parent.mkdir(parents=True)
        dst.write_text("user edited", encoding="utf-8")

        # 配布時ハッシュは "distributed: v1"（現在の "user edited" とは異なる
        # = install 後にユーザーが変更した状態を再現）
        orch = {
            "installed_packages": ["mypkg"],
            "file_hashes": {"mypkg": {"config/mypkg/foo.yaml": _sha256("distributed: v1")}},
        }
        (claude_dir / "orchestra.json").write_text(json.dumps(orch), encoding="utf-8")

        # source を dst より確実に新しくする（ガード無しなら needs_sync=True で上書きされる）
        future = time.time() + 10
        os.utime(pkg_dir / "config" / "foo.yaml", (future, future))

        manager = OrchestraManager(orchestra_dir)
        manager.run_initial_sync(project_dir, dry_run=False)

        assert dst.read_text(encoding="utf-8") == "user edited"

    def test_hash_match_still_syncs_when_source_is_newer(self, tmp_path: Path, monkeypatch) -> None:
        """対照テスト: ハッシュが一致（未変更）なら従来どおり同期される。"""
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        pkg_dir = orchestra_dir / "packages" / "mypkg"
        (pkg_dir / "config").mkdir(parents=True)
        (pkg_dir / "config" / "foo.yaml").write_text("distributed: v2", encoding="utf-8")
        _write_manifest(orchestra_dir / "packages", "mypkg", config=["config/foo.yaml"])
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(orchestra_dir))

        project_dir = tmp_path / "project"
        claude_dir = project_dir / ".claude"
        dst = claude_dir / "config" / "mypkg" / "foo.yaml"
        dst.parent.mkdir(parents=True)
        dst.write_text("distributed: v1", encoding="utf-8")

        orch = {
            "installed_packages": ["mypkg"],
            "file_hashes": {"mypkg": {"config/mypkg/foo.yaml": _sha256("distributed: v1")}},
        }
        (claude_dir / "orchestra.json").write_text(json.dumps(orch), encoding="utf-8")

        future = time.time() + 10
        os.utime(pkg_dir / "config" / "foo.yaml", (future, future))

        manager = OrchestraManager(orchestra_dir)
        manager.run_initial_sync(project_dir, dry_run=False)

        assert dst.read_text(encoding="utf-8") == "distributed: v2"
