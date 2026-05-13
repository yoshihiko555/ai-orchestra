"""composition と manifest の整合性テスト（ADR-015 準拠）。

`facets/compositions/{skills,rules}/*.yaml` の全 composition は、
いずれかの `packages/*/manifest.json` の `skills` / `rules` 配列に
登録されていなければならない。

孤立した composition（manifest 未登録）はパッケージのライフサイクル管理
から外れるため、ADR-015 で禁止された。
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _composition_names(comp_type: str) -> list[str]:
    comp_dir = REPO_ROOT / "facets" / "compositions" / comp_type
    names: list[str] = []
    for path in sorted(comp_dir.rglob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            name = data.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def _manifest_registrations(key: str) -> set[str]:
    registered: set[str] = set()
    for manifest in (REPO_ROOT / "packages").glob("*/manifest.json"):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for entry in data.get(key, []):
            if isinstance(entry, str):
                registered.add(entry)
    return registered


def test_no_orphan_skill_compositions() -> None:
    compositions = set(_composition_names("skills"))
    registered = _manifest_registrations("skills")
    orphans = sorted(compositions - registered)
    assert not orphans, (
        f"以下のスキルがどの packages/*/manifest.json にも登録されていません: {orphans}\n"
        "ADR-015 に従い packages/<pkg>/manifest.json の skills 配列に追加してください。"
    )


def test_no_orphan_rule_compositions() -> None:
    compositions = set(_composition_names("rules"))
    registered = _manifest_registrations("rules")
    orphans = sorted(compositions - registered)
    assert not orphans, (
        f"以下のルールがどの packages/*/manifest.json にも登録されていません: {orphans}\n"
        "ADR-015 に従い packages/<pkg>/manifest.json の rules 配列に追加してください。"
    )
