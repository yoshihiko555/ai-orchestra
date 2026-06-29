"""codd.py CLI（scan / graph / validate）の unit test。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

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


def test_scan_respects_recursive_exclude_glob(tmp_path) -> None:
    # exclude も Path.glob で解決するため、`**` 再帰 glob が直下・ネスト両方を除外する。
    _write(tmp_path, "docs/a.md", _doc("design:a", "design"))
    _write(tmp_path, "docs/sub/b.md", _doc("design:b", "design"))
    _write(tmp_path, "guides/c.md", _doc("design:c", "design"))
    config = _config(
        scope={"include": ["docs/**/*.md", "guides/**/*.md"], "exclude": ["docs/**/*.md"]}
    )
    result = cli.scan_project(tmp_path, config)
    ids = {n.node_id for n in result.nodes}
    assert ids == {"design:c"}


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


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def test_commit_time_clean_uses_git_dirty_uses_mtime(tmp_path) -> None:
    # クリーン追跡ファイルは git コミット時刻、未コミット編集のあるファイルは mtime。
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "tester")
    target = _write(tmp_path, "docs/x.md", _doc("design:x", "design"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    future = 4102444800.0  # 2100-01-01。mtime を未来へ置き、コミット時刻と区別する
    os.utime(target, (future, future))
    # クリーン: コミット時刻が返る（mtime ではない）
    assert cli.commit_time(tmp_path, "docs/x.md") != future

    # 編集して dirty にする → mtime が返る（drift を取りこぼさない）
    target.write_text(_doc("design:x", "design") + "\nedit\n", encoding="utf-8")
    os.utime(target, (future, future))
    assert cli.commit_time(tmp_path, "docs/x.md") == future


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


# ---------------------------------------------------------------------------
# impact 分析（compute_impact: pure ロジック）
# ---------------------------------------------------------------------------


def _node(node_id: str, kind: str = "design", deps: list[tuple[str, str]] | None = None):
    return cc.CoddNode(
        node_id=node_id,
        kind=kind,
        status="draft",
        depends_on=tuple(cc.Dependency(id=i, relation=r) for i, r in (deps or [])),
        owner=None,
        path=f"docs/{node_id.replace(':', '_')}.md",
    )


def _impact_cfg(**overrides):
    return cc.ImpactConfig.from_dict(overrides)


def _by_id(impacted) -> dict:
    return {n.node_id: n for n in impacted}


def test_impact_direct_strong_is_green() -> None:
    graph = cc.build_graph(
        [_node("req:r", "requirement"), _node("design:d", deps=[("req:r", "derives_from")])]
    )
    impacted = _by_id(cc.compute_impact(graph, {"req:r"}, _impact_cfg()))
    assert set(impacted) == {"design:d"}
    node = impacted["design:d"]
    assert node.band == cc.BAND_GREEN
    assert node.score == 1.0
    assert node.min_hops == 1
    assert node.origins == ["req:r"]
    assert node.co_changed is False


def test_impact_references_is_gray() -> None:
    graph = cc.build_graph(
        [_node("req:r", "requirement"), _node("design:d", deps=[("req:r", "references")])]
    )
    impacted = _by_id(cc.compute_impact(graph, {"req:r"}, _impact_cfg()))
    assert impacted["design:d"].band == cc.BAND_GRAY
    assert impacted["design:d"].score == 0.3


def test_impact_two_hop_strong_is_amber() -> None:
    graph = cc.build_graph(
        [
            _node("req:r", "requirement"),
            _node("design:d", deps=[("req:r", "derives_from")]),
            _node("plan:p", "plan", deps=[("design:d", "implements")]),
        ]
    )
    impacted = _by_id(cc.compute_impact(graph, {"req:r"}, _impact_cfg()))
    assert impacted["design:d"].band == cc.BAND_GREEN  # 1 hop
    assert impacted["plan:p"].band == cc.BAND_AMBER  # 2 hop → 0.5
    assert impacted["plan:p"].score == 0.5
    assert impacted["plan:p"].min_hops == 2


def test_impact_supersedes_direct_is_amber() -> None:
    graph = cc.build_graph(
        [
            _node("adr:old", "adr"),
            _node("adr:new", "adr", deps=[("adr:old", "supersedes")]),
        ]
    )
    impacted = _by_id(cc.compute_impact(graph, {"adr:old"}, _impact_cfg()))
    assert impacted["adr:new"].band == cc.BAND_AMBER
    assert impacted["adr:new"].score == 0.6


def test_impact_co_changed_caps_at_amber() -> None:
    graph = cc.build_graph(
        [_node("req:r", "requirement"), _node("design:d", deps=[("req:r", "derives_from")])]
    )
    # design 自身も変更済み → 直接強依存で Green になるはずが Amber 上限へ。
    impacted = _by_id(cc.compute_impact(graph, {"req:r", "design:d"}, _impact_cfg()))
    node = impacted["design:d"]
    assert node.co_changed is True
    assert node.band == cc.BAND_AMBER
    assert node.score == 1.0  # スコア自体は下げない（破壊変更を Gray に隠さない）


def test_impact_corroboration_downgrades_uncorroborated_green() -> None:
    # decay=1.0 で 2-hop でも score 1.0（推論的 Green 候補）を作る。
    cfg = _impact_cfg(decay=1.0)
    graph = cc.build_graph(
        [
            _node("req:a", "requirement"),
            _node("design:m", deps=[("req:a", "derives_from")]),
            _node("plan:leaf", "plan", deps=[("design:m", "implements")]),
        ]
    )
    impacted = _by_id(cc.compute_impact(graph, {"req:a"}, cfg))
    # 単一起点・直接事実でない 2-hop は Green に上げず Amber へ降格。
    assert impacted["plan:leaf"].score == 1.0
    assert impacted["plan:leaf"].band == cc.BAND_AMBER
    # 直接強依存の design:m は Green のまま。
    assert impacted["design:m"].band == cc.BAND_GREEN


def test_impact_corroboration_allows_multi_origin_green() -> None:
    cfg = _impact_cfg(decay=1.0)
    graph = cc.build_graph(
        [
            _node("req:a", "requirement"),
            _node("req:b", "requirement"),
            _node("design:m", deps=[("req:a", "derives_from"), ("req:b", "derives_from")]),
            _node("plan:leaf", "plan", deps=[("design:m", "implements")]),
        ]
    )
    impacted = _by_id(cc.compute_impact(graph, {"req:a", "req:b"}, cfg))
    leaf = impacted["plan:leaf"]
    # 2 起点が裏付ける → Corroboration を満たし Green を許可。
    assert sorted(leaf.origins) == ["req:a", "req:b"]
    assert leaf.band == cc.BAND_GREEN


def test_impact_cycle_safe() -> None:
    # a <-> b の相互依存。無限ループせず下流を一度だけ列挙する。
    graph = cc.build_graph(
        [
            _node("design:a", deps=[("design:b", "refines")]),
            _node("design:b", deps=[("design:a", "refines")]),
        ]
    )
    impacted = _by_id(cc.compute_impact(graph, {"design:a"}, _impact_cfg()))
    assert "design:b" in impacted
    # 起点 a は自身の下流ではない（サイクルでも origin は影響先に含めない）。
    assert "design:a" not in impacted


def test_impact_empty_changed_ids() -> None:
    graph = cc.build_graph(
        [_node("req:r", "requirement"), _node("design:d", deps=[("req:r", "derives_from")])]
    )
    assert cc.compute_impact(graph, set(), _impact_cfg()) == []


def test_impact_origin_not_in_graph() -> None:
    # 変更ノードがグラフ未登録（scope 外の変更など）でも例外を出さず空を返す。
    graph = cc.build_graph([_node("design:d", deps=[("req:r", "derives_from")])])
    assert cc.compute_impact(graph, {"req:nonexistent"}, _impact_cfg()) == []


def test_impact_respects_max_hops() -> None:
    graph = cc.build_graph(
        [
            _node("req:a", "requirement"),
            _node("design:b", deps=[("req:a", "derives_from")]),
            _node("design:c", deps=[("design:b", "refines")]),
            _node("plan:d", "plan", deps=[("design:c", "implements")]),
        ]
    )
    impacted = _by_id(cc.compute_impact(graph, {"req:a"}, _impact_cfg(max_hops=2)))
    assert "design:b" in impacted  # 1 hop
    assert "design:c" in impacted  # 2 hop
    assert "plan:d" not in impacted  # 3 hop → 打ち切り


def test_impact_unknown_relation_uses_fallback_weight() -> None:
    graph = cc.build_graph(
        [_node("req:r", "requirement"), _node("design:d", deps=[("req:r", "weird")])]
    )
    impacted = _by_id(cc.compute_impact(graph, {"req:r"}, _impact_cfg()))
    # 未知 relation は弱依存フォールバック（0.3）→ Gray。
    assert impacted["design:d"].score == cc.UNKNOWN_RELATION_WEIGHT
    assert impacted["design:d"].band == cc.BAND_GRAY


# ---------------------------------------------------------------------------
# impact 分析（CLI: git 連携）
# ---------------------------------------------------------------------------


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "tester")


def test_compute_impact_result_maps_diff_to_downstream(tmp_path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    # 上流 req を未コミット編集 → diff HEAD に出る
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement") + "\n変更\n")

    result = cli.compute_impact_result(tmp_path, _config(), "HEAD")
    assert result.changed_ids == ["req:r"]
    impacted = _by_id(result.impacted)
    assert impacted["design:d"].band == cc.BAND_GREEN


def test_compute_impact_result_reports_deleted_upstream(tmp_path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "docs/req.md").unlink()  # 上流削除（未コミット）

    result = cli.compute_impact_result(tmp_path, _config(), "HEAD")
    assert "docs/req.md" in result.deleted_upstream
    assert result.changed_ids == []  # 削除されたファイルはノードに残らない


def test_deleted_upstream_excludes_out_of_scope_md(tmp_path) -> None:
    # scope は docs/**/*.md。スコープ外の README.md 削除は dangling 注意に含めない。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(tmp_path, "README.md", "# readme\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "docs/req.md").unlink()
    (tmp_path / "README.md").unlink()

    result = cli.compute_impact_result(tmp_path, _config(), "HEAD")
    assert "docs/req.md" in result.deleted_upstream
    assert "README.md" not in result.deleted_upstream  # スコープ外を誤検出しない


def test_path_in_scope() -> None:
    config = _config(scope={"include": ["docs/**/*.md"], "exclude": ["docs/adr/_template.md"]})
    assert cli.path_in_scope("docs/x.md", config) is True
    assert cli.path_in_scope("docs/sub/y.md", config) is True
    assert cli.path_in_scope("README.md", config) is False  # スコープ外
    assert cli.path_in_scope("docs/x.txt", config) is False  # 拡張子不一致
    assert cli.path_in_scope("docs/adr/_template.md", config) is False  # exclude


def test_impact_config_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"decay": 2.0})  # (0, 1] 外
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"max_hops": 0})  # 1 未満
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"green_threshold": 0.3, "amber_threshold": 0.5})  # 帯域逆転


def test_cmd_impact_json_output(tmp_path, capsys) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement") + "\nx\n")

    assert cli.cmd_impact(tmp_path, _config(), "HEAD", as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ref"] == "HEAD"
    assert payload["changed_nodes"] == ["req:r"]
    entry = {e["node_id"]: e for e in payload["impacted"]}["design:d"]
    assert entry["band"] == cc.BAND_GREEN
    assert entry["origins"] == ["req:r"]
