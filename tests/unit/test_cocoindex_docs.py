"""cocoindex パッケージのドキュメント突合テスト。

`packages/cocoindex` は hook 型パッケージだが、以下の観点は実行コードではなく
ドキュメント記述そのものが期待仕様のため、ドキュメント drift を検出する
「文書とテストの突合チェック」として実装する（docs/evaluation/cocoindex.md）。

対象観点:
- EV-14（should）: v1（stdio）モードで複数 CLI が同一プロジェクトの MCP サーバーを
  同時起動すると SQLite ロック競合が発生し得る（本パッケージが自動解消することは
  保証しない既知の制限）
- EV-17（should）: `docs/reference/packages.md` の hook 一覧と
  `packages/cocoindex/manifest.json` の `hooks` 定義が一致する
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES_MD = REPO_ROOT / "docs" / "reference" / "packages.md"
MANIFEST_JSON = REPO_ROOT / "packages" / "cocoindex" / "manifest.json"
COCOINDEX_USAGE_MD = REPO_ROOT / "facets" / "instructions" / "cocoindex-usage.md"
EVAL_MD = REPO_ROOT / "docs" / "evaluation" / "cocoindex.md"


def _extract_package_section(markdown: str, heading: str) -> str:
    """`## {heading}` から次の `## ` 見出し（または EOF）までの本文を抽出する。"""
    pattern = re.compile(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    m = pattern.search(markdown)
    assert m is not None, f"'## {heading}' セクションが見つからない: {markdown[:200]}"
    return m.group(1)


def _extract_hook_names_from_component_table(section: str) -> set[str]:
    """コンポーネント表（種別 | 名前 | 説明）から種別が hook の行の名前を抽出する。"""
    names: set[str] = set()
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        kind, name = cells[0], cells[1]
        if kind != "hook":
            continue
        m = re.search(r"`([^`]+)`", name)
        if m:
            names.add(m.group(1))
    return names


def _flatten_manifest_hook_files(hooks: dict) -> set[str]:
    """manifest.json の hooks 定義（イベント種別ごとの list）からファイル名集合を抽出する。

    要素は文字列（ファイル名そのもの）または {"file": ..., "timeout": ...} の dict。
    """
    files: set[str] = set()
    for entries in hooks.values():
        for entry in entries:
            if isinstance(entry, dict):
                files.add(entry["file"])
            else:
                files.add(entry)
    return files


class TestPackagesDocMatchesManifest:
    """EV-17: docs/reference/packages.md の hook 一覧と manifest.json の hooks 定義が一致する。"""

    def test_hook_list_matches(self) -> None:
        packages_md = PACKAGES_MD.read_text(encoding="utf-8")
        section = _extract_package_section(packages_md, "cocoindex")
        documented_hooks = _extract_hook_names_from_component_table(section)

        manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
        manifest_hooks = _flatten_manifest_hook_files(manifest["hooks"])

        assert documented_hooks == manifest_hooks

    def test_manifest_files_include_all_declared_hooks(self) -> None:
        """manifest.json の hooks で参照されるファイルは files リストにも含まれる。"""
        manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
        manifest_hooks = _flatten_manifest_hook_files(manifest["hooks"])
        declared_files = {Path(f).name for f in manifest["files"]}

        assert manifest_hooks <= declared_files


class TestSqliteLimitationDocumented:
    """EV-14: v1（stdio）の SQLite ロック競合は既知の制限として文書化されている。"""

    def test_cocoindex_usage_documents_v1_sqlite_conflict(self) -> None:
        content = COCOINDEX_USAGE_MD.read_text(encoding="utf-8")
        assert "SQLite" in content
        assert "ロック競合" in content
        assert "現在の回避策（v1）" in content

    def test_evaluation_set_non_goals_document_no_auto_resolution(self) -> None:
        """評価セットの Non-Goals に「自動解消しない」既知の制限が明記されている。"""
        content = EVAL_MD.read_text(encoding="utf-8")
        assert "SQLite ロック競合の完全自動解消" in content
