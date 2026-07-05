"""sync_engine.sync_codex_files() のユニットテスト。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# sync_engine は scripts/ からの相対 import を使うため sys.path にスクリプトルートを追加
_repo_root = Path(__file__).resolve().parents[2]
_scripts_dir = str(_repo_root / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from tests.module_loader import load_module

sync_engine = load_module("sync_engine_codex_files", "scripts/lib/sync_engine.py")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_manifest(pkg_dir: Path, codex_files: list[dict[str, str]]) -> None:
    manifest = {"name": pkg_dir.name, "codex_files": codex_files}
    (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class TestSyncCodexFilesNewTarget:
    """target が存在しない場合の同期。"""

    def test_copies_and_records_hash(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "codex-harness"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "codex").mkdir()
        source = pkg_dir / "codex" / "hooks.json"
        source.write_text('{"hooks": []}', encoding="utf-8")
        _write_manifest(pkg_dir, [{"source": "codex/hooks.json", "target": ".codex/hooks.json"}])

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        orch: dict = {}

        count = sync_engine.sync_codex_files(project_dir, orchestra_path, ["codex-harness"], orch)

        target = project_dir / ".codex" / "hooks.json"
        assert count == 1
        assert target.read_text(encoding="utf-8") == '{"hooks": []}'
        assert orch["codex_file_hashes"][".codex/hooks.json"] == _sha256('{"hooks": []}')

    def test_missing_source_is_skipped(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "codex-harness"
        pkg_dir.mkdir(parents=True)
        _write_manifest(pkg_dir, [{"source": "codex/missing.json", "target": ".codex/x.json"}])

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        orch: dict = {}

        count = sync_engine.sync_codex_files(project_dir, orchestra_path, ["codex-harness"], orch)

        assert count == 0
        assert not (project_dir / ".codex" / "x.json").exists()

    def test_missing_manifest_is_skipped(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        (orchestra_path / "packages" / "no-manifest").mkdir(parents=True)

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        orch: dict = {}

        count = sync_engine.sync_codex_files(project_dir, orchestra_path, ["no-manifest"], orch)
        assert count == 0


class TestSyncCodexFilesUnchangedTarget:
    """target が存在し、記録ハッシュと現ハッシュが一致する場合。"""

    def _setup(self, tmp_path: Path, source_text: str) -> tuple[Path, Path, dict]:
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "codex-harness"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "codex").mkdir()
        source = pkg_dir / "codex" / "hooks.json"
        source.write_text(source_text, encoding="utf-8")
        _write_manifest(pkg_dir, [{"source": "codex/hooks.json", "target": ".codex/hooks.json"}])

        project_dir = tmp_path / "project"
        (project_dir / ".codex").mkdir(parents=True)
        target = project_dir / ".codex" / "hooks.json"
        target.write_text(source_text, encoding="utf-8")
        orch = {"codex_file_hashes": {".codex/hooks.json": _sha256(source_text)}}
        return orchestra_path, project_dir, orch

    def test_no_op_when_source_unchanged(self, tmp_path: Path) -> None:
        orchestra_path, project_dir, orch = self._setup(tmp_path, '{"hooks": []}')

        count = sync_engine.sync_codex_files(project_dir, orchestra_path, ["codex-harness"], orch)

        assert count == 0
        assert orch["codex_file_hashes"][".codex/hooks.json"] == _sha256('{"hooks": []}')

    def test_updates_when_source_changed(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "codex-harness"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "codex").mkdir()
        source = pkg_dir / "codex" / "hooks.json"
        source.write_text('{"hooks": ["new"]}', encoding="utf-8")
        _write_manifest(pkg_dir, [{"source": "codex/hooks.json", "target": ".codex/hooks.json"}])

        project_dir = tmp_path / "project"
        (project_dir / ".codex").mkdir(parents=True)
        target = project_dir / ".codex" / "hooks.json"
        target.write_text('{"hooks": []}', encoding="utf-8")
        orch = {"codex_file_hashes": {".codex/hooks.json": _sha256('{"hooks": []}')}}

        count = sync_engine.sync_codex_files(project_dir, orchestra_path, ["codex-harness"], orch)

        assert count == 1
        assert target.read_text(encoding="utf-8") == '{"hooks": ["new"]}'
        assert orch["codex_file_hashes"][".codex/hooks.json"] == _sha256('{"hooks": ["new"]}')


class TestSyncCodexFilesUserModified:
    """target が存在し、現ハッシュが記録ハッシュと一致しない（ユーザー改変）場合。"""

    def _setup(self, tmp_path: Path) -> tuple[Path, Path, dict]:
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "codex-harness"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "codex").mkdir()
        source = pkg_dir / "codex" / "hooks.json"
        source.write_text('{"hooks": ["new"]}', encoding="utf-8")
        _write_manifest(pkg_dir, [{"source": "codex/hooks.json", "target": ".codex/hooks.json"}])

        project_dir = tmp_path / "project"
        (project_dir / ".codex").mkdir(parents=True)
        target = project_dir / ".codex" / "hooks.json"
        target.write_text('{"hooks": ["user-edited"]}', encoding="utf-8")
        orch = {"codex_file_hashes": {".codex/hooks.json": _sha256('{"hooks": []}')}}
        return orchestra_path, project_dir, orch

    def test_skips_without_force(self, tmp_path: Path, capsys) -> None:
        orchestra_path, project_dir, orch = self._setup(tmp_path)
        target = project_dir / ".codex" / "hooks.json"

        count = sync_engine.sync_codex_files(project_dir, orchestra_path, ["codex-harness"], orch)

        assert count == 0
        assert target.read_text(encoding="utf-8") == '{"hooks": ["user-edited"]}'
        assert "warn" in capsys.readouterr().err

    def test_overwrites_with_force(self, tmp_path: Path) -> None:
        orchestra_path, project_dir, orch = self._setup(tmp_path)
        target = project_dir / ".codex" / "hooks.json"

        count = sync_engine.sync_codex_files(
            project_dir, orchestra_path, ["codex-harness"], orch, force=True
        )

        assert count == 1
        assert target.read_text(encoding="utf-8") == '{"hooks": ["new"]}'
        assert orch["codex_file_hashes"][".codex/hooks.json"] == _sha256('{"hooks": ["new"]}')


class TestSyncCodexFilesUntrackedExisting:
    """記録が無いのに target が既に存在する（初回導入前の既存ファイル）場合。"""

    def _setup(self, tmp_path: Path) -> tuple[Path, Path, dict]:
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "codex-harness"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "codex").mkdir()
        source = pkg_dir / "codex" / "hooks.json"
        source.write_text('{"hooks": ["new"]}', encoding="utf-8")
        _write_manifest(pkg_dir, [{"source": "codex/hooks.json", "target": ".codex/hooks.json"}])

        project_dir = tmp_path / "project"
        (project_dir / ".codex").mkdir(parents=True)
        target = project_dir / ".codex" / "hooks.json"
        target.write_text('{"hooks": ["pre-existing"]}', encoding="utf-8")
        orch: dict = {}
        return orchestra_path, project_dir, orch

    def test_skips_without_force(self, tmp_path: Path, capsys) -> None:
        orchestra_path, project_dir, orch = self._setup(tmp_path)
        target = project_dir / ".codex" / "hooks.json"

        count = sync_engine.sync_codex_files(project_dir, orchestra_path, ["codex-harness"], orch)

        assert count == 0
        assert target.read_text(encoding="utf-8") == '{"hooks": ["pre-existing"]}'
        assert "warn" in capsys.readouterr().err

    def test_overwrites_with_force(self, tmp_path: Path) -> None:
        orchestra_path, project_dir, orch = self._setup(tmp_path)
        target = project_dir / ".codex" / "hooks.json"

        count = sync_engine.sync_codex_files(
            project_dir, orchestra_path, ["codex-harness"], orch, force=True
        )

        assert count == 1
        assert target.read_text(encoding="utf-8") == '{"hooks": ["new"]}'
        assert ".codex/hooks.json" in orch["codex_file_hashes"]


class TestSyncCodexFilesPathTraversal:
    """target がプロジェクト外を指す場合（絶対パス・../ 脱出）は同期しない（H5）。"""

    def test_skips_absolute_path_target(self, tmp_path: Path, capsys) -> None:
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "codex-harness"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "codex").mkdir()
        source = pkg_dir / "codex" / "hooks.json"
        source.write_text('{"hooks": []}', encoding="utf-8")

        outside_target = tmp_path / "outside" / "escaped.json"
        _write_manifest(pkg_dir, [{"source": "codex/hooks.json", "target": str(outside_target)}])

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        orch: dict = {}

        count = sync_engine.sync_codex_files(project_dir, orchestra_path, ["codex-harness"], orch)

        assert count == 0
        assert not outside_target.exists()
        assert "warn" in capsys.readouterr().err

    def test_skips_dot_dot_escape_target(self, tmp_path: Path, capsys) -> None:
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "codex-harness"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "codex").mkdir()
        source = pkg_dir / "codex" / "hooks.json"
        source.write_text('{"hooks": []}', encoding="utf-8")
        _write_manifest(
            pkg_dir,
            [{"source": "codex/hooks.json", "target": "../../outside/escaped.json"}],
        )

        project_dir = tmp_path / "nested" / "project"
        project_dir.mkdir(parents=True)
        orch: dict = {}

        count = sync_engine.sync_codex_files(project_dir, orchestra_path, ["codex-harness"], orch)

        assert count == 0
        assert not (tmp_path / "outside" / "escaped.json").exists()
        assert "warn" in capsys.readouterr().err


class TestCollectFacetBuildTargets:
    """collect_facet_build_targets() のテスト。"""

    def test_always_includes_claude(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        orchestra_path.mkdir()
        assert sync_engine.collect_facet_build_targets(orchestra_path, []) == ["claude"]
        assert sync_engine.collect_facet_build_targets(orchestra_path, None) == ["claude"]

    def test_adds_codex_when_declared(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "codex-suggestions"
        pkg_dir.mkdir(parents=True)
        manifest = {"name": "codex-suggestions", "facet_targets": ["codex"]}
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        targets = sync_engine.collect_facet_build_targets(orchestra_path, ["codex-suggestions"])
        assert targets == ["claude", "codex"]

    def test_no_facet_targets_field_stays_claude_only(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "core"
        pkg_dir.mkdir(parents=True)
        manifest = {"name": "core"}
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        targets = sync_engine.collect_facet_build_targets(orchestra_path, ["core"])
        assert targets == ["claude"]

    def test_deduplicates_across_packages(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        for pkg_name in ("pkg-a", "pkg-b"):
            pkg_dir = orchestra_path / "packages" / pkg_name
            pkg_dir.mkdir(parents=True)
            manifest = {"name": pkg_name, "facet_targets": ["codex"]}
            (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        targets = sync_engine.collect_facet_build_targets(orchestra_path, ["pkg-a", "pkg-b"])
        assert targets == ["claude", "codex"]
