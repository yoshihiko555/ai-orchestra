"""E2E テスト: 配布物への Facet 同梱。"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT, requires_writable_repo

# CI では dev extras（hatchling / hatch-vcs）が欠けても #399 の回帰検知が skip で黙って
# 消えないよう、import 失敗をそのまま収集エラーにする。hatch_vcs はビルドフックとして
# hatchling から読み込まれるだけでコードからは参照しないため、存在確認のみ行う。
# ローカルは dev extras を入れ直していなくても e2e を回せるよう skip する。
if os.getenv("CI"):
    importlib.import_module("hatch_vcs")
else:
    pytest.importorskip("hatchling")
    pytest.importorskip("hatch_vcs")

from hatchling.builders.sdist import SdistBuilder
from hatchling.builders.wheel import WheelBuilder

pytestmark = requires_writable_repo

# orchestra root から実行時に参照されるルート（EV-42）。wheel では ai_orchestra/ 配下、
# sdist ではトップディレクトリ（orchex-<version>）直下に入る。
RUNTIME_ROOTS = ("packages", "templates", "scripts", "facets", "presets.json")
WHEEL_PACKAGE_PREFIX = "ai_orchestra/"
COMPOSITIONS_DIRECTORY = "facets/compositions/"
YAML_SUFFIX = ".yaml"
MAX_REPORTED_MISSING = 10
SANITY_IMPORT_SCRIPT = (
    "import ai_orchestra; import ai_orchestra.cli as c; "
    "print(ai_orchestra.__file__); print(c.get_orchestra_dir())"
)


@dataclass(frozen=True)
class BuiltDistributions:
    """ビルド済みの wheel と sdist。"""

    wheel_path: Path
    sdist_path: Path


@pytest.fixture(scope="module")
def built_distributions(tmp_path_factory: pytest.TempPathFactory) -> BuiltDistributions:
    """wheel と sdist をモジュール内で一度だけビルドする。"""
    out_dir = tmp_path_factory.mktemp("distributions")
    wheel_path = next(
        iter(WheelBuilder(str(REPO_ROOT)).build(directory=str(out_dir), versions=["standard"]))
    )
    sdist_path = next(
        iter(SdistBuilder(str(REPO_ROOT)).build(directory=str(out_dir), versions=["standard"]))
    )
    return BuiltDistributions(wheel_path=Path(wheel_path), sdist_path=Path(sdist_path))


def _path_in_sdist(member_path: str) -> str:
    """sdist メンバーからバージョンで変わるトップディレクトリを除いたパスを返す。"""
    _, _, path_in_sdist = member_path.partition("/")
    return path_in_sdist


def _tracked_runtime_files() -> set[str]:
    """実行時ルート配下で git 管理されているファイル（REPO_ROOT 相対）を返す。"""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *RUNTIME_ROOTS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    # index にあっても作業ツリーで削除中のファイルはビルドに入らないため除く
    return {path for path in result.stdout.split("\0") if path and (REPO_ROOT / path).is_file()}


def _assert_runtime_roots(relative_paths: list[str]) -> None:
    """実行時ルート配下の git 管理ファイルが、配布物にすべて含まれている。

    ディレクトリの有無だけで判定しない。sdist の include にある "CLAUDE.md" 等は
    ディレクトリ内の同名ファイルにも一致するため、templates/ を外しても
    templates/project/CLAUDE.md だけは残り、有無判定では欠落を見逃す。
    """
    tracked_files = _tracked_runtime_files()
    assert any(
        path.startswith(COMPOSITIONS_DIRECTORY) and path.endswith(YAML_SUFFIX)
        for path in tracked_files
    ), "git ls-files で composition YAML が見つからない（比較の前提が崩れている）"
    missing_files = sorted(tracked_files - set(relative_paths))
    assert not missing_files, (
        f"{len(missing_files)} files missing: {missing_files[:MAX_REPORTED_MISSING]}"
    )


def _subprocess_output(process: subprocess.CompletedProcess[str]) -> str:
    """サブプロセス失敗時に標準出力と標準エラーを表示する。"""
    return f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}"


def _parse_sanity_paths(process: subprocess.CompletedProcess[str]) -> tuple[Path, Path]:
    """sanity-check の標準出力から検証対象のパスを読み取る。"""
    try:
        package_file, orchestra_dir = process.stdout.strip().splitlines()
    except ValueError:
        pytest.fail(_subprocess_output(process))
    return Path(package_file), Path(orchestra_dir)


class TestPackaging:
    """wheel と sdist の配布内容を検証する。"""

    def test_wheel_contains_runtime_assets(self, built_distributions: BuiltDistributions) -> None:
        """#399: wheel の ai_orchestra/ 配下に実行時に参照される全ルートと composition が含まれる。"""
        with zipfile.ZipFile(built_distributions.wheel_path) as archive:
            member_paths = archive.namelist()

        _assert_runtime_roots(
            [
                member_path.removeprefix(WHEEL_PACKAGE_PREFIX)
                for member_path in member_paths
                if member_path.startswith(WHEEL_PACKAGE_PREFIX)
            ]
        )

    def test_sdist_contains_runtime_assets(self, built_distributions: BuiltDistributions) -> None:
        """#399: sdist 直下に実行時に参照される全ルートと composition が含まれる。

        sdist から wheel を再ビルドする環境では、sdist に無いルートは wheel にも入らない。
        """
        with tarfile.open(built_distributions.sdist_path, mode="r:gz") as archive:
            member_paths = archive.getnames()

        _assert_runtime_roots([_path_in_sdist(member_path) for member_path in member_paths])

    def test_facet_build_uses_installed_wheel_assets(
        self,
        built_distributions: BuiltDistributions,
        e2e_project: Path,
        tmp_path: Path,
    ) -> None:
        """#399: 抽出した wheel だけで facet build が成功する。"""
        extract_dir = tmp_path / "wheel"
        extract_dir.mkdir()
        with zipfile.ZipFile(built_distributions.wheel_path) as archive:
            archive.extractall(extract_dir)

        env = os.environ.copy()
        env.pop("AI_ORCHESTRA_DIR", None)
        env.pop("PYTHONPATH", None)
        env["PYTHONPATH"] = str(extract_dir)

        sanity_process = subprocess.run(
            [sys.executable, "-P", "-c", SANITY_IMPORT_SCRIPT],
            cwd=str(extract_dir),
            env=env,
            capture_output=True,
            text=True,
        )
        sanity_output = _subprocess_output(sanity_process)
        assert sanity_process.returncode == 0, sanity_output
        package_file, orchestra_dir = _parse_sanity_paths(sanity_process)
        extract_root = extract_dir.resolve()
        assert package_file.resolve().is_relative_to(extract_root), sanity_output
        assert orchestra_dir.resolve().is_relative_to(extract_root), sanity_output

        env["HOME"] = str(e2e_project)
        setup_process = subprocess.run(
            [
                sys.executable,
                "-P",
                "-m",
                "ai_orchestra.cli",
                "setup",
                "essential",
                "--project",
                str(e2e_project),
            ],
            cwd=str(extract_dir),
            env=env,
            capture_output=True,
            text=True,
        )
        assert setup_process.returncode == 0, _subprocess_output(setup_process)

        build_process = subprocess.run(
            [
                sys.executable,
                "-P",
                "-m",
                "ai_orchestra.cli",
                "facet",
                "build",
                "--project",
                str(e2e_project),
            ],
            cwd=str(extract_dir),
            env=env,
            capture_output=True,
            text=True,
        )
        assert build_process.returncode == 0, _subprocess_output(build_process)
        assert (e2e_project / ".claude" / "skills" / "review" / "SKILL.md").is_file()
        assert (e2e_project / ".claude" / "rules" / "coding-principles.md").is_file()
