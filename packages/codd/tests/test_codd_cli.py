"""codd.py CLI（scan / graph / validate）の unit test。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from tests.module_loader import load_module

cc = load_module("codd_common", "packages/codd/lib/codd_common.py")
cli = load_module("codd_cli", "packages/codd/scripts/codd.py")


BASE_CONFIG = {
    "enabled": True,
    "scope": {"include": ["docs/**/*.md"], "exclude": []},
    "kinds": ["requirement", "design", "adr", "plan", "rule", "instruction"],
    "relations": ["derives_from", "refines", "implements", "references", "supersedes"],
    "roots": ["requirement", "instruction"],
    "graph_store": {"format": "jsonl", "path": ".claude/codd/graph.jsonl"},
    "checks": {
        "dangling": "error",
        "duplicate": "error",
        "cycle": "error",
        "unknown": "error",
        "missing_frontmatter": "warning",
        "orphan": "warning",
        "drift": "warning",
    },
}


def _config(**overrides) -> object:
    data = {**BASE_CONFIG, **overrides}
    return cc.CoddConfig.from_dict(data)


def _doc(
    node_id: str,
    kind: str,
    status: str = "draft",
    deps: list[tuple[str, str]] | None = None,
) -> str:
    lines = ["---", "codd:", f"  node_id: {node_id}", f"  kind: {kind}", f"  status: {status}"]
    if deps:
        lines.append("  depends_on:")
        for dep_id, relation in deps:
            lines.append(f"    - id: {dep_id}")
            lines.append(f"      relation: {relation}")
    lines += ["---", "", "# 本文", ""]
    return "\n".join(lines)


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# scan / graph 永続化
# ---------------------------------------------------------------------------


def test_scan_project_builds_graph(tmp_path) -> None:
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    result = cli.scan_project(tmp_path, _config())
    assert len(result.nodes) == 2
    assert result.graph.has("req:r")
    assert result.graph.incoming_count("req:r") == 1
    assert result.missing_frontmatter == []


def test_scan_records_missing_frontmatter(tmp_path) -> None:
    _write(tmp_path, "docs/has.md", _doc("design:d", "design"))
    _write(tmp_path, "docs/none.md", "# フロントマター無し\n")
    result = cli.scan_project(tmp_path, _config())
    assert result.missing_frontmatter == ["docs/none.md"]


def test_scan_respects_exclude(tmp_path) -> None:
    _write(tmp_path, "docs/keep.md", _doc("design:k", "design"))
    _write(tmp_path, "docs/skip.md", _doc("design:s", "design"))
    config = _config(scope={"include": ["docs/**/*.md"], "exclude": ["docs/skip.md"]})
    result = cli.scan_project(tmp_path, config)
    ids = {n.node_id for n in result.nodes}
    assert ids == {"design:k"}


def test_write_graph_jsonl_roundtrip(tmp_path) -> None:
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    result = cli.scan_project(tmp_path, _config())
    out = tmp_path / ".claude/codd/graph.jsonl"
    cli.write_graph_jsonl(result, out)
    records = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert records[0]["node_id"] == "design:d"
    assert records[0]["depends_on"] == [{"id": "req:r", "relation": "derives_from"}]


# ---------------------------------------------------------------------------
# validate 検査
# ---------------------------------------------------------------------------


def _checks(result, config, root) -> dict[str, list]:
    findings = cli.run_checks(result, config, root)
    grouped: dict[str, list] = {}
    for f in findings:
        grouped.setdefault(f.check, []).append(f)
    return grouped


def test_validate_clean(tmp_path) -> None:
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    result = cli.scan_project(tmp_path, _config())
    findings = cli.run_checks(result, _config(), tmp_path)
    errors = [f for f in findings if f.level == cc.LEVEL_ERROR]
    assert errors == []


def test_validate_dangling(tmp_path) -> None:
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:missing", "derives_from")]),
    )
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    assert len(grouped["dangling"]) == 1
    assert grouped["dangling"][0].level == cc.LEVEL_ERROR


def test_validate_duplicate(tmp_path) -> None:
    _write(tmp_path, "docs/a.md", _doc("design:dup", "design"))
    _write(tmp_path, "docs/b.md", _doc("design:dup", "design"))
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    assert len(grouped["duplicate"]) == 1


def test_validate_cycle(tmp_path) -> None:
    _write(tmp_path, "docs/a.md", _doc("design:a", "design", deps=[("design:b", "refines")]))
    _write(tmp_path, "docs/b.md", _doc("design:b", "design", deps=[("design:a", "refines")]))
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    assert len(grouped["cycle"]) == 1


def test_validate_unknown_kind_and_relation_and_status(tmp_path) -> None:
    _write(tmp_path, "docs/k.md", _doc("x:k", "widget"))
    _write(
        tmp_path,
        "docs/r.md",
        _doc("design:r", "design", status="bogus", deps=[("x:k", "weird_rel")]),
    )
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    messages = " ".join(f.message for f in grouped["unknown"])
    assert "widget" in messages
    assert "weird_rel" in messages
    assert "bogus" in messages


def test_validate_unknown_flags_empty_status(tmp_path) -> None:
    # status: 欠落（空）も unknown error 扱い（5 プロパティ必須）。
    doc = "---\ncodd:\n  node_id: design:s\n  kind: design\n---\n# body\n"
    _write(tmp_path, "docs/s.md", doc)
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    messages = " ".join(f.message for f in grouped.get("unknown", []))
    assert "status" in messages


def test_validate_unknown_flags_empty_node_id(tmp_path) -> None:
    doc = "---\ncodd:\n  kind: design\n  status: draft\n---\n# body\n"
    _write(tmp_path, "docs/n.md", doc)
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    messages = " ".join(f.message for f in grouped.get("unknown", []))
    assert "node_id" in messages


def test_validate_missing_frontmatter_warning(tmp_path) -> None:
    _write(tmp_path, "docs/none.md", "# no frontmatter\n")
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    assert len(grouped["missing_frontmatter"]) == 1
    assert grouped["missing_frontmatter"][0].level == cc.LEVEL_WARNING


def test_validate_orphan_warning_excludes_roots(tmp_path) -> None:
    # requirement は roots なので孤立でも除外。design は orphan として検出される。
    _write(tmp_path, "docs/root.md", _doc("req:root", "requirement"))
    _write(tmp_path, "docs/lonely.md", _doc("design:lonely", "design"))
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    orphans = grouped.get("orphan", [])
    assert len(orphans) == 1
    assert orphans[0].level == cc.LEVEL_WARNING
    assert "docs/lonely.md" in orphans[0].message


def test_validate_drift_warning_via_mtime(tmp_path) -> None:
    # tmp_path は git 管理外 → commit_time は mtime にフォールバック。
    down = _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    up = _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    # 上流 (req) を下流 (design) より新しくする → drift
    os.utime(down, (1000, 1000))
    os.utime(up, (2000, 2000))
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    assert len(grouped["drift"]) == 1
    assert grouped["drift"][0].level == cc.LEVEL_WARNING


def test_checks_off_level_suppresses(tmp_path) -> None:
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:missing", "derives_from")]),
    )
    result = cli.scan_project(tmp_path, _config())
    config = _config(checks={**BASE_CONFIG["checks"], "dangling": "off"})
    findings = cli.run_checks(result, config, tmp_path)
    assert all(f.check != "dangling" for f in findings)


def test_validate_exit_codes(tmp_path) -> None:
    # error あり → 1
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:missing", "derives_from")]),
    )
    assert cli.cmd_validate(tmp_path, _config()) == 1
    # error 無し（warning のみ）→ 0
    (tmp_path / "docs/design.md").unlink()
    _write(tmp_path, "docs/lonely.md", _doc("design:lonely", "design"))
    assert cli.cmd_validate(tmp_path, _config()) == 0
