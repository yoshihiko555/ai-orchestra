"""orchestra-manager.py の --force フラグ配線テスト。

テスト対象:
- CLI `install --force` が manager.install(..., force=True) に配線されること
- run_initial_sync(force=True) が sync_codex_files に force を伝播し、
  配布後に改変された codex_files も上書きすること
- run_initial_sync が apply_codex_harness_config を呼び出すこと
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

from tests.module_loader import load_module

manager_mod = load_module("orchestra_manager_force", "scripts/orchestra-manager.py")
OrchestraManager = manager_mod.OrchestraManager


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_orchestra_with_codex_harness(orchestra_dir: Path) -> None:
    pkg_dir = orchestra_dir / "packages" / "codex-harness"
    (pkg_dir / "codex").mkdir(parents=True)
    (pkg_dir / "codex" / "hooks.json").write_text('{"hooks": {"new": true}}', encoding="utf-8")
    manifest = {
        "name": "codex-harness",
        "version": "0.1.0",
        "description": "",
        "depends": [],
        "hooks": {},
        "codex_files": [{"source": "codex/hooks.json", "target": ".codex/hooks.json"}],
    }
    (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class TestRunInitialSyncForce:
    def test_force_overwrites_modified_codex_file(self, tmp_path: Path, monkeypatch) -> None:
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        _make_orchestra_with_codex_harness(orchestra_dir)
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(orchestra_dir))

        project_dir = tmp_path / "project"
        (project_dir / ".claude").mkdir(parents=True)
        codex_dir = project_dir / ".codex"
        codex_dir.mkdir()
        (codex_dir / "hooks.json").write_text('{"hooks": {"tampered": true}}', encoding="utf-8")

        orch = {
            "installed_packages": ["codex-harness"],
            "codex_file_hashes": {".codex/hooks.json": _sha256('{"hooks": {"old": true}}')},
        }
        (project_dir / ".claude" / "orchestra.json").write_text(json.dumps(orch), encoding="utf-8")

        manager = OrchestraManager(orchestra_dir)
        manager.run_initial_sync(project_dir, dry_run=False, force=True)

        assert (codex_dir / "hooks.json").read_text(encoding="utf-8") == '{"hooks": {"new": true}}'

    def test_without_force_skips_modified_codex_file(self, tmp_path: Path, monkeypatch) -> None:
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        _make_orchestra_with_codex_harness(orchestra_dir)
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(orchestra_dir))

        project_dir = tmp_path / "project"
        (project_dir / ".claude").mkdir(parents=True)
        codex_dir = project_dir / ".codex"
        codex_dir.mkdir()
        (codex_dir / "hooks.json").write_text('{"hooks": {"tampered": true}}', encoding="utf-8")

        orch = {
            "installed_packages": ["codex-harness"],
            "codex_file_hashes": {".codex/hooks.json": _sha256('{"hooks": {"old": true}}')},
        }
        (project_dir / ".claude" / "orchestra.json").write_text(json.dumps(orch), encoding="utf-8")

        manager = OrchestraManager(orchestra_dir)
        manager.run_initial_sync(project_dir, dry_run=False, force=False)

        assert (codex_dir / "hooks.json").read_text(
            encoding="utf-8"
        ) == '{"hooks": {"tampered": true}}'

    def test_calls_apply_codex_harness_config(self, tmp_path: Path, monkeypatch) -> None:
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        _make_orchestra_with_codex_harness(orchestra_dir)
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(orchestra_dir))

        project_dir = tmp_path / "project"
        (project_dir / ".claude").mkdir(parents=True)
        (project_dir / ".codex").mkdir()
        (project_dir / ".claude" / "orchestra.json").write_text(
            json.dumps({"installed_packages": ["codex-harness"]}), encoding="utf-8"
        )

        manager = OrchestraManager(orchestra_dir)
        with patch.object(manager_mod, "apply_codex_harness_config") as mock_apply:
            mock_apply.return_value = False
            manager.run_initial_sync(project_dir, dry_run=False)

        mock_apply.assert_called_once()


class TestUninstallCodexFiles:
    """EV-48: uninstall() は codex_files（配布時ハッシュ一致分）を削除すること。"""

    def _make_project(self, tmp_path: Path, hooks_json_content: str) -> tuple[Path, Path]:
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        _make_orchestra_with_codex_harness(orchestra_dir)

        project_dir = tmp_path / "project"
        (project_dir / ".claude").mkdir(parents=True)
        codex_dir = project_dir / ".codex"
        codex_dir.mkdir()
        (codex_dir / "hooks.json").write_text(hooks_json_content, encoding="utf-8")
        return orchestra_dir, project_dir

    def test_removes_unmodified_codex_file_and_ledger_entry(self, tmp_path: Path) -> None:
        content = '{"hooks": {"new": true}}'
        orchestra_dir, project_dir = self._make_project(tmp_path, content)
        orch = {
            "installed_packages": ["codex-harness"],
            "codex_file_hashes": {".codex/hooks.json": _sha256(content)},
        }
        (project_dir / ".claude" / "orchestra.json").write_text(json.dumps(orch), encoding="utf-8")

        manager = OrchestraManager(orchestra_dir)
        manager.uninstall("codex-harness", str(project_dir), dry_run=False)

        assert not (project_dir / ".codex" / "hooks.json").exists()
        saved_orch = json.loads(
            (project_dir / ".claude" / "orchestra.json").read_text(encoding="utf-8")
        )
        assert ".codex/hooks.json" not in saved_orch.get("codex_file_hashes", {})
        assert "codex-harness" not in saved_orch.get("installed_packages", [])

    def test_keeps_user_modified_codex_file_and_warns(self, tmp_path: Path, capsys) -> None:
        orchestra_dir, project_dir = self._make_project(tmp_path, '{"hooks": {"tampered": true}}')
        orch = {
            "installed_packages": ["codex-harness"],
            "codex_file_hashes": {".codex/hooks.json": _sha256('{"hooks": {"old": true}}')},
        }
        (project_dir / ".claude" / "orchestra.json").write_text(json.dumps(orch), encoding="utf-8")

        manager = OrchestraManager(orchestra_dir)
        manager.uninstall("codex-harness", str(project_dir), dry_run=False)

        assert (project_dir / ".codex" / "hooks.json").read_text(
            encoding="utf-8"
        ) == '{"hooks": {"tampered": true}}'
        assert "警告" in capsys.readouterr().out
        saved_orch = json.loads(
            (project_dir / ".claude" / "orchestra.json").read_text(encoding="utf-8")
        )
        assert saved_orch.get("codex_file_hashes", {}).get(".codex/hooks.json") == _sha256(
            '{"hooks": {"old": true}}'
        )

    def test_dry_run_does_not_delete_or_mutate_ledger(self, tmp_path: Path) -> None:
        content = '{"hooks": {"new": true}}'
        orchestra_dir, project_dir = self._make_project(tmp_path, content)
        orch = {
            "installed_packages": ["codex-harness"],
            "codex_file_hashes": {".codex/hooks.json": _sha256(content)},
        }
        (project_dir / ".claude" / "orchestra.json").write_text(json.dumps(orch), encoding="utf-8")

        manager = OrchestraManager(orchestra_dir)
        manager.uninstall("codex-harness", str(project_dir), dry_run=True)

        assert (project_dir / ".codex" / "hooks.json").exists()
        saved_orch = json.loads(
            (project_dir / ".claude" / "orchestra.json").read_text(encoding="utf-8")
        )
        assert ".codex/hooks.json" in saved_orch.get("codex_file_hashes", {})


class TestInstallCliForceFlag:
    def test_install_command_parses_and_forwards_force(self, tmp_path: Path, monkeypatch) -> None:
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)

        captured: dict = {}

        def fake_install(
            self, package_name, project, dry_run=False, _skip_dep_check=False, force=False
        ):
            captured["package_name"] = package_name
            captured["force"] = force

        monkeypatch.setattr(manager_mod.OrchestraManager, "install", fake_install)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "orchestra-manager.py",
                "--orchestra-dir",
                str(orchestra_dir),
                "install",
                "foo",
                "--force",
            ],
        )

        manager_mod.main()

        assert captured == {"package_name": "foo", "force": True}

    def test_install_command_defaults_force_to_false(self, tmp_path: Path, monkeypatch) -> None:
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)

        captured: dict = {}

        def fake_install(
            self, package_name, project, dry_run=False, _skip_dep_check=False, force=False
        ):
            captured["force"] = force

        monkeypatch.setattr(manager_mod.OrchestraManager, "install", fake_install)
        monkeypatch.setattr(
            sys,
            "argv",
            ["orchestra-manager.py", "--orchestra-dir", str(orchestra_dir), "install", "foo"],
        )

        manager_mod.main()

        assert captured == {"force": False}
