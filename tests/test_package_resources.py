"""全パッケージの manifest.json リソース整合性テスト。

テスト観点:
- manifest の agents/skills/rules エントリが実ファイル（またはディレクトリ）として存在する
- パッケージ内に存在する agents/skills/rules ファイルが manifest に登録されている（孤立検出）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = REPO_ROOT / "packages"

# 全パッケージの manifest をロード
_MANIFESTS: dict[str, dict] = {}
for _manifest_path in sorted(PACKAGES_DIR.glob("*/manifest.json")):
    with open(_manifest_path, "r", encoding="utf-8") as _f:
        _MANIFESTS[_manifest_path.parent.name] = json.load(_f)


# ---------------------------------------------------------------------------
# manifest エントリの実在チェック
# ---------------------------------------------------------------------------

_RESOURCE_ENTRIES: list[tuple[str, str, str]] = []
for _pkg_name, _manifest in _MANIFESTS.items():
    for _category in ("agents", "skills", "rules"):
        for _entry in _manifest.get(_category, []):
            _RESOURCE_ENTRIES.append((_pkg_name, _category, _entry))


class TestManifestEntriesExist:
    """manifest に記載されたリソースが実在するか。"""

    @pytest.mark.parametrize(
        "pkg_name,category,entry",
        _RESOURCE_ENTRIES,
        ids=[f"{p}/{e}" for p, _, e in _RESOURCE_ENTRIES],
    )
    def test_resource_exists(self, pkg_name: str, category: str, entry: str) -> None:
        path = PACKAGES_DIR / pkg_name / entry
        assert path.exists(), (
            f"packages/{pkg_name}/manifest.json の {category} エントリ "
            f"'{entry}' が存在しません: {path}"
        )


# ---------------------------------------------------------------------------
# 孤立ファイル検出
# ---------------------------------------------------------------------------


def _collect_actual_resources(pkg_name: str) -> set[str]:
    """パッケージ内の agents/skills/rules ファイルを収集。"""
    pkg_dir = PACKAGES_DIR / pkg_name
    actual: set[str] = set()

    for category in ("agents", "skills", "rules"):
        cat_dir = pkg_dir / category
        if not cat_dir.is_dir():
            continue

        if category == "agents":
            # agents はフラットな .md ファイル
            for f in cat_dir.glob("*.md"):
                actual.add(f"{category}/{f.name}")
        elif category == "skills":
            # skills はディレクトリ単位（SKILL.md を含むディレクトリ）
            for skill_dir in cat_dir.iterdir():
                if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                    actual.add(f"{category}/{skill_dir.name}")
        elif category == "rules":
            # rules はフラットな .md ファイル
            for f in cat_dir.glob("*.md"):
                actual.add(f"{category}/{f.name}")

    return actual


def _collect_manifest_resources(manifest: dict) -> set[str]:
    """manifest から agents/skills/rules エントリを収集。"""
    entries: set[str] = set()
    for category in ("agents", "skills", "rules"):
        for entry in manifest.get(category, []):
            entries.add(entry)
    return entries


_PACKAGES_WITH_RESOURCES = [
    pkg_name
    for pkg_name, manifest in _MANIFESTS.items()
    if _collect_actual_resources(pkg_name)
]


class TestNoOrphanResources:
    """パッケージ内に存在するが manifest に未登録のリソースがないか。"""

    @pytest.mark.parametrize("pkg_name", _PACKAGES_WITH_RESOURCES)
    def test_no_orphan_files(self, pkg_name: str) -> None:
        actual = _collect_actual_resources(pkg_name)
        registered = _collect_manifest_resources(_MANIFESTS[pkg_name])
        orphans = actual - registered
        assert not orphans, (
            f"packages/{pkg_name} に manifest 未登録のリソース: {sorted(orphans)}"
        )
