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

    def test_dry_run_previews_codex_files_sync_without_writing(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """R20: dry_run=True must still preview codex_files sync (no actual copy)."""
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        _make_orchestra_with_codex_harness(orchestra_dir)
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(orchestra_dir))

        project_dir = tmp_path / "project"
        (project_dir / ".claude").mkdir(parents=True)
        (project_dir / ".claude" / "orchestra.json").write_text(
            json.dumps({"installed_packages": ["codex-harness"]}), encoding="utf-8"
        )

        manager = OrchestraManager(orchestra_dir)
        manager.run_initial_sync(project_dir, dry_run=True)

        assert not (project_dir / ".codex" / "hooks.json").exists()
        assert "[DRY-RUN]" in capsys.readouterr().out

    def test_dry_run_previews_config_merge_without_writing(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """R20: dry_run=True must preview .codex/config.toml merge without writing it."""
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        _make_orchestra_with_codex_harness(orchestra_dir)
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(orchestra_dir))

        project_dir = tmp_path / "project"
        (project_dir / ".claude").mkdir(parents=True)
        (project_dir / ".codex").mkdir()
        config_path = project_dir / ".codex" / "config.toml"
        original_config = 'model = "gpt-5.5"\n'
        config_path.write_text(original_config, encoding="utf-8")
        (project_dir / ".claude" / "orchestra.json").write_text(
            json.dumps({"installed_packages": ["codex-harness"]}), encoding="utf-8"
        )

        manager = OrchestraManager(orchestra_dir)
        with patch.object(manager_mod, "apply_codex_harness_config") as mock_apply:
            mock_apply.return_value = True
            manager.run_initial_sync(project_dir, dry_run=True)

        mock_apply.assert_called_once()
        assert mock_apply.call_args.args[0] == project_dir
        assert mock_apply.call_args.args[2] == ["codex-harness"]
        assert mock_apply.call_args.kwargs == {"dry_run": True}
        assert config_path.read_text(encoding="utf-8") == original_config

    def test_continues_when_config_merge_raises_toml_merge_error(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """R19: apply_codex_harness_config raising TomlMergeError must not crash
        run_initial_sync (already fail-soft in the implementation; this pins it)."""
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
        with patch.object(
            manager_mod,
            "apply_codex_harness_config",
            side_effect=manager_mod.TomlMergeError("bad merge"),
        ):
            manager.run_initial_sync(project_dir, dry_run=False)

        assert "警告" in capsys.readouterr().err

    def test_continues_when_config_merge_raises_toml_decode_error(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """R19: same as above but for tomllib.TOMLDecodeError."""
        import tomllib

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
        with patch.object(
            manager_mod,
            "apply_codex_harness_config",
            side_effect=tomllib.TOMLDecodeError("bad toml", "doc", 0),
        ):
            manager.run_initial_sync(project_dir, dry_run=False)

        assert "警告" in capsys.readouterr().err

    def test_continues_when_config_merge_raises_os_error(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """R19: same as above but for OSError (e.g. permission denied on write)."""
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
        with patch.object(
            manager_mod,
            "apply_codex_harness_config",
            side_effect=OSError("permission denied"),
        ):
            manager.run_initial_sync(project_dir, dry_run=False)

        assert "警告" in capsys.readouterr().err


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


class TestUninstallCodexFilesPathTraversal:
    """R13: codex_files.target がプロジェクト外を指す場合は uninstall 側でも削除しない。"""

    def _make_manifest_with_target(self, orchestra_dir: Path, target: str) -> None:
        pkg_dir = orchestra_dir / "packages" / "codex-harness"
        (pkg_dir / "codex").mkdir(parents=True)
        (pkg_dir / "codex" / "hooks.json").write_text('{"hooks": {"new": true}}', encoding="utf-8")
        manifest = {
            "name": "codex-harness",
            "version": "0.1.0",
            "description": "",
            "depends": [],
            "hooks": {},
            "codex_files": [{"source": "codex/hooks.json", "target": target}],
        }
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_skips_dot_dot_escape_target(self, tmp_path: Path, capsys) -> None:
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        escape_target = "../../outside/escaped.json"
        self._make_manifest_with_target(orchestra_dir, escape_target)

        project_dir = tmp_path / "nested" / "project"
        (project_dir / ".claude").mkdir(parents=True)
        outside_file = tmp_path / "outside" / "escaped.json"
        outside_file.parent.mkdir(parents=True)
        outside_file.write_text('{"hooks": {"pre-existing": true}}', encoding="utf-8")

        orch = {
            "installed_packages": ["codex-harness"],
            "codex_file_hashes": {escape_target: _sha256('{"hooks": {"pre-existing": true}}')},
        }
        (project_dir / ".claude" / "orchestra.json").write_text(json.dumps(orch), encoding="utf-8")

        manager = OrchestraManager(orchestra_dir)
        manager.uninstall("codex-harness", str(project_dir), dry_run=False)

        assert outside_file.exists()
        assert "警告" in capsys.readouterr().out
        saved_orch = json.loads(
            (project_dir / ".claude" / "orchestra.json").read_text(encoding="utf-8")
        )
        assert escape_target in saved_orch.get("codex_file_hashes", {})

    def test_skips_absolute_path_target(self, tmp_path: Path, capsys) -> None:
        orchestra_dir = tmp_path / "orchestra"
        (orchestra_dir / "packages").mkdir(parents=True)
        outside_file = tmp_path / "outside" / "escaped.json"
        outside_file.parent.mkdir(parents=True)
        outside_file.write_text('{"hooks": {"pre-existing": true}}', encoding="utf-8")
        self._make_manifest_with_target(orchestra_dir, str(outside_file))

        project_dir = tmp_path / "project"
        (project_dir / ".claude").mkdir(parents=True)

        orch = {
            "installed_packages": ["codex-harness"],
            "codex_file_hashes": {str(outside_file): _sha256('{"hooks": {"pre-existing": true}}')},
        }
        (project_dir / ".claude" / "orchestra.json").write_text(json.dumps(orch), encoding="utf-8")

        manager = OrchestraManager(orchestra_dir)
        manager.uninstall("codex-harness", str(project_dir), dry_run=False)

        assert outside_file.exists()
        assert "警告" in capsys.readouterr().out


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
