"""orchestra-manager.py のコア機能テスト。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.module_loader import load_module

manager_mod = load_module("orchestra_manager", "scripts/orchestra-manager.py")
OrchestraManager = manager_mod.OrchestraManager
models_mod = load_module("orchestra_models", "scripts/orchestra_models.py")
Package = models_mod.Package


def _make_manager(orchestra_dir: Path) -> OrchestraManager:
    """テスト用 OrchestraManager を生成する。"""
    (orchestra_dir / "packages").mkdir(parents=True, exist_ok=True)
    return OrchestraManager(orchestra_dir)


def _write_manifest(
    packages_dir: Path, name: str, depends: list[str] | None = None, hooks: dict | None = None
) -> Path:
    """packages/<name>/manifest.json を作成して manifest_path を返す。"""
    pkg_dir = packages_dir / name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "name": name,
        "version": "1.0.0",
        "description": "",
        "depends": depends or [],
        "hooks": hooks or {},
    }
    manifest_path = pkg_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


class TestCopyTemplateIfMissing:
    def test_copies_when_dst_absent_returns_true(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        src = tmp_path / "src.md"
        src.write_text("hello", encoding="utf-8")
        dst = tmp_path / "dst.md"

        # Act
        result = manager._copy_template_if_missing(src, dst, label="test")

        # Assert
        assert result is True
        assert dst.exists()
        assert dst.read_text(encoding="utf-8") == "hello"

    def test_skips_when_dst_exists_returns_false(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        src = tmp_path / "src.md"
        src.write_text("src content", encoding="utf-8")
        dst = tmp_path / "dst.md"
        dst.write_text("existing", encoding="utf-8")

        # Act
        result = manager._copy_template_if_missing(src, dst, label="test")

        # Assert
        assert result is False
        assert dst.read_text(encoding="utf-8") == "existing"

    def test_returns_false_when_src_absent(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        src = tmp_path / "nonexistent.md"
        dst = tmp_path / "dst.md"

        # Act
        result = manager._copy_template_if_missing(src, dst, label="test")

        # Assert
        assert result is False
        assert not dst.exists()

    def test_dry_run_does_not_create_file_returns_true(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        src = tmp_path / "src.md"
        src.write_text("hello", encoding="utf-8")
        dst = tmp_path / "dst.md"

        # Act
        result = manager._copy_template_if_missing(src, dst, label="test", dry_run=True)

        # Assert
        assert result is True
        assert not dst.exists()


class TestResolveInstallOrder:
    def test_no_deps_returns_sorted_order(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        _write_manifest(tmp_path / "packages", "pkgB")
        _write_manifest(tmp_path / "packages", "pkgA")

        # Act
        result = manager.resolve_install_order(["pkgB", "pkgA"])

        # Assert
        assert result == ["pkgA", "pkgB"]

    def test_respects_dependency_order(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        _write_manifest(tmp_path / "packages", "pkgA")
        _write_manifest(tmp_path / "packages", "pkgB", depends=["pkgA"])

        # Act
        result = manager.resolve_install_order(["pkgB", "pkgA"])

        # Assert
        assert result.index("pkgA") < result.index("pkgB")

    def test_cycle_falls_back_to_original_order(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        _write_manifest(tmp_path / "packages", "pkgA", depends=["pkgB"])
        _write_manifest(tmp_path / "packages", "pkgB", depends=["pkgA"])
        original = ["pkgB", "pkgA"]

        # Act
        result = manager.resolve_install_order(original)

        # Assert
        assert result == original


class TestGetPackageStatus:
    def test_returns_installed_when_in_installed_packages_with_no_hooks(
        self, tmp_path: Path
    ) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        manifest_path = _write_manifest(tmp_path / "packages", "mypkg")
        pkg = Package.load(manifest_path)
        orch = {"installed_packages": ["mypkg"], "orchestra_dir": "", "last_sync": ""}
        settings: dict = {"hooks": {}}

        # Act
        status, registered, total = manager.get_package_status(
            pkg, tmp_path, orch=orch, settings=settings, all_packages={"mypkg": pkg}
        )

        # Assert
        assert status == "installed"

    def test_returns_installed_when_all_hooks_registered(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        manifest_path = _write_manifest(
            tmp_path / "packages", "mypkg", hooks={"SessionStart": ["hook.py"]}
        )
        pkg = Package.load(manifest_path)
        orch = {"installed_packages": ["mypkg"], "orchestra_dir": "", "last_sync": ""}
        settings: dict = {"hooks": {}}
        manager.add_hook_to_settings(settings, "SessionStart", "hook.py", "mypkg")

        # Act
        status, registered, total = manager.get_package_status(
            pkg, tmp_path, orch=orch, settings=settings, all_packages={"mypkg": pkg}
        )

        # Assert
        assert status == "installed"
        assert registered == total == 1

    def test_returns_partial_when_some_hooks_missing(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        manifest_path = _write_manifest(
            tmp_path / "packages",
            "mypkg",
            hooks={"SessionStart": ["hook_a.py", "hook_b.py"]},
        )
        pkg = Package.load(manifest_path)
        orch = {"installed_packages": ["mypkg"], "orchestra_dir": "", "last_sync": ""}
        settings: dict = {"hooks": {}}
        # Register only one of two hooks
        manager.add_hook_to_settings(settings, "SessionStart", "hook_a.py", "mypkg")

        # Act
        status, registered, total = manager.get_package_status(
            pkg, tmp_path, orch=orch, settings=settings, all_packages={"mypkg": pkg}
        )

        # Assert
        assert status == "partial"
        assert registered == 1
        assert total == 2

    def test_returns_not_found_when_not_installed_no_hooks_registered(self, tmp_path: Path) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        manifest_path = _write_manifest(
            tmp_path / "packages", "mypkg", hooks={"SessionStart": ["hook.py"]}
        )
        pkg = Package.load(manifest_path)
        orch = {"installed_packages": [], "orchestra_dir": "", "last_sync": ""}
        settings: dict = {"hooks": {}}

        # Act
        status, registered, total = manager.get_package_status(
            pkg, tmp_path, orch=orch, settings=settings, all_packages={"mypkg": pkg}
        )

        # Assert
        assert status == "not found"

    def test_returns_active_when_not_installed_but_dependent_is_installed(
        self, tmp_path: Path
    ) -> None:
        # Arrange
        manager = _make_manager(tmp_path)
        # base_pkg: no hooks, not installed
        base_manifest = _write_manifest(tmp_path / "packages", "base_pkg")
        base_pkg = Package.load(base_manifest)
        # dep_pkg: depends on base_pkg, installed
        dep_manifest = _write_manifest(tmp_path / "packages", "dep_pkg", depends=["base_pkg"])
        dep_pkg = Package.load(dep_manifest)

        orch = {"installed_packages": ["dep_pkg"], "orchestra_dir": "", "last_sync": ""}
        settings: dict = {"hooks": {}}
        all_packages = {"base_pkg": base_pkg, "dep_pkg": dep_pkg}

        # Act
        status, registered, total = manager.get_package_status(
            base_pkg,
            tmp_path,
            orch=orch,
            settings=settings,
            all_packages=all_packages,
        )

        # Assert
        assert status == "active"
