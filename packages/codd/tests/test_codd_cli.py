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


def test_validate_unknown_flags_node_id_without_colon(tmp_path) -> None:
    # EV-12: node_id は `<kind>:<file-slug>` 形式。コロンが無ければ unknown error。
    doc = (
        "---\ncodd:\n  node_id: designwithoutcolon\n  kind: design\n  status: draft\n---\n# body\n"
    )
    _write(tmp_path, "docs/s.md", doc)
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    messages = " ".join(f.message for f in grouped.get("unknown", []))
    assert "designwithoutcolon" in messages
    assert "コロン無し" in messages


def test_validate_unknown_flags_kind_node_id_prefix_mismatch(tmp_path) -> None:
    # EV-12: kind は正しい語彙だが node_id のプレフィックスが kind と対応しない
    # （例: kind=requirement なのに node_id は "design:" プレフィックス）。
    _write(tmp_path, "docs/req.md", _doc("design:mismatched", "requirement"))
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    messages = " ".join(f.message for f in grouped.get("unknown", []))
    assert "design:mismatched" in messages
    assert "不一致" in messages


def test_validate_accepts_requirement_abbreviated_req_prefix(tmp_path) -> None:
    # EV-12: requirement kind は "req" プレフィックスへ略記される（設計 4.3 の表）。
    # これは不一致ではなく正常系として通ること。
    _write(tmp_path, "docs/req.md", _doc("req:coherence-guardrail", "requirement"))
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    assert grouped.get("unknown", []) == []


def test_validate_node_id_without_colon_reports_single_finding(tmp_path) -> None:
    # Codex レビュー反映: 形式不正（コロン無し）と kind プレフィックス不一致を
    # 二重報告しない（1 ノードにつき unknown finding は 1 件のみ）。
    doc = (
        "---\ncodd:\n  node_id: designwithoutcolon\n  kind: design\n  status: draft\n---\n# body\n"
    )
    _write(tmp_path, "docs/s.md", doc)
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    node_id_findings = [f for f in grouped.get("unknown", []) if "designwithoutcolon" in f.message]
    assert len(node_id_findings) == 1


def test_validate_flags_node_id_with_empty_prefix_or_slug(tmp_path) -> None:
    # EV-12: プレフィックス側・スラッグ側どちらが空でも `<kind>:<file-slug>` 形式でない。
    _write(
        tmp_path,
        "docs/a.md",
        '---\ncodd:\n  node_id: ":a"\n  kind: design\n  status: draft\n---\n# body\n',
    )
    _write(
        tmp_path,
        "docs/b.md",
        '---\ncodd:\n  node_id: "design:"\n  kind: design\n  status: draft\n---\n# body\n',
    )
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    messages = [f.message for f in grouped.get("unknown", [])]
    assert sum("コロン無し" in m for m in messages) == 2


def test_validate_unmapped_custom_kind_skips_prefix_check(tmp_path) -> None:
    # Codex レビュー反映: config.kinds に NODE_ID_PREFIX_BY_KIND 未定義の独自 kind を
    # 追加しても、プレフィックス照合をスキップし誤検知（false positive）しない。
    _write(tmp_path, "docs/e.md", _doc("eval:e", "eval", status="draft"))
    config = _config(kinds=[*BASE_CONFIG["kinds"], "eval"])
    result = cli.scan_project(tmp_path, config)
    grouped = _checks(result, config, tmp_path)
    messages = " ".join(f.message for f in grouped.get("unknown", []))
    assert "プレフィックス" not in messages  # 未マッピング kind は照合スキップ（誤検知しない）


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


def test_band_for_score_boundaries() -> None:
    # EV-15: score>=0.8 は Green、>=0.4 は Amber、それ未満は Gray（境界値そのもの）。
    cfg = _impact_cfg()
    assert cc._band_for_score(0.8, cfg) == cc.BAND_GREEN  # ちょうど green_threshold
    assert cc._band_for_score(0.7999, cfg) == cc.BAND_AMBER  # green 未満は amber
    assert cc._band_for_score(0.4, cfg) == cc.BAND_AMBER  # ちょうど amber_threshold
    assert cc._band_for_score(0.3999, cfg) == cc.BAND_GRAY  # amber 未満は gray


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
    assert result.impacted == []  # 削除は deleted_upstream に分離され impacted には出ない


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
    assert result.impacted == []  # 削除は deleted_upstream に分離され impacted には出ない


def test_path_in_scope() -> None:
    config = _config(scope={"include": ["docs/**/*.md"], "exclude": ["docs/adr/_template.md"]})
    assert cli.path_in_scope("docs/x.md", config) is True
    assert cli.path_in_scope("docs/sub/y.md", config) is True
    assert cli.path_in_scope("README.md", config) is False  # スコープ外
    assert cli.path_in_scope("docs/x.txt", config) is False  # 拡張子不一致
    assert cli.path_in_scope("docs/adr/_template.md", config) is False  # exclude


def test_path_in_scope_single_star_is_segment_aware() -> None:
    # 単層 glob (dir/*.md) は 1 セグメントのみ。サブディレクトリを跨いではならない。
    config = _config(scope={"include": [".claude/rules/*.md"], "exclude": []})
    assert cli.path_in_scope(".claude/rules/foo.md", config) is True
    assert cli.path_in_scope(".claude/rules/sub/deep.md", config) is False  # 単層を跨がない


def test_impact_config_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"decay": 2.0})  # (0, 1] 外
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"max_hops": 0})  # 1 未満
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"green_threshold": 0.3, "amber_threshold": 0.5})  # 帯域逆転
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"green_threshold": 1.5})  # [0, 1] 外
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"amber_threshold": -0.1})  # [0, 1] 外
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"corroboration_min_origins": 0})  # 1 未満


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


def test_rename_keeps_node_out_of_deleted_upstream(tmp_path) -> None:
    # rename で node_id が新パスに引き継がれた上流は dangling 警告に出さない。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    _git(tmp_path, "mv", "docs/req.md", "docs/req2.md")  # node_id は req:r のまま移動

    result = cli.compute_impact_result(tmp_path, _config(), "HEAD")
    assert "docs/req.md" not in result.deleted_upstream  # 移動しただけ → dangling ではない
    assert result.deleted_upstream == []
    assert "req:r" in result.changed_ids  # 移動先は changed 扱い → 下流が影響を受ける


def test_compute_impact_result_raises_on_invalid_ref(tmp_path) -> None:
    # 無効な ref / git 失敗を空の成功結果にせず、明示的にエラーへ昇格させる。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    with pytest.raises(cli.ImpactError):
        cli.compute_impact_result(tmp_path, _config(), "no-such-ref")


def test_cmd_impact_returns_nonzero_on_invalid_ref(tmp_path, capsys) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    assert cli.cmd_impact(tmp_path, _config(), "no-such-ref", as_json=False) == 2
    assert "ERROR" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# diff_changed_paths（非 ASCII パス / quotePath 対応）
# ---------------------------------------------------------------------------


def test_diff_changed_paths_detects_non_ascii_modification(tmp_path) -> None:
    # core.quotePath=true（既定）を明示設定し、-z パースが quote/エスケープの影響を
    # 受けないことを再現性を持って確認する。
    _init_repo(tmp_path)
    _git(tmp_path, "config", "core.quotePath", "true")
    target = _write(tmp_path, "docs/日本語ドキュメント.md", _doc("design:jp", "design"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    target.write_text(_doc("design:jp", "design") + "\n変更\n", encoding="utf-8")

    changed, deleted = cli.diff_changed_paths(tmp_path, "HEAD")
    assert "docs/日本語ドキュメント.md" in changed
    assert deleted == set()


def test_diff_changed_paths_detects_non_ascii_rename(tmp_path) -> None:
    _init_repo(tmp_path)
    _git(tmp_path, "config", "core.quotePath", "true")
    _write(tmp_path, "docs/旧名前.md", _doc("design:jp", "design"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    _git(tmp_path, "mv", "docs/旧名前.md", "docs/新しい名前.md")

    changed, deleted = cli.diff_changed_paths(tmp_path, "HEAD")
    assert "docs/旧名前.md" in deleted
    assert "docs/新しい名前.md" in changed


def test_diff_changed_paths_detects_non_ascii_deletion(tmp_path) -> None:
    _init_repo(tmp_path)
    _git(tmp_path, "config", "core.quotePath", "true")
    target = _write(tmp_path, "docs/削除予定.md", _doc("design:jp", "design"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    target.unlink()

    changed, deleted = cli.diff_changed_paths(tmp_path, "HEAD")
    assert "docs/削除予定.md" in deleted
    assert "docs/削除予定.md" not in changed


# ---------------------------------------------------------------------------
# EV-19: 依存宣言の正本はフロントマター1箇所のみ（外部サイドカーの二重管理否定）
# ---------------------------------------------------------------------------


def test_codd_config_has_no_external_links_field() -> None:
    # EV-19: 依存宣言の正本はフロントマター1箇所のみ。config スキーマに doc_links
    # 等の外部依存宣言ファイルを指すフィールドが存在しないことを確認する
    # （「存在しないこと」の裏付け）。
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(cc.CoddConfig)}
    forbidden = {"doc_links", "links_file", "dependencies_file", "deps_path", "links_path"}
    assert field_names.isdisjoint(forbidden)


def test_scan_ignores_external_doc_links_sidecar(tmp_path) -> None:
    # EV-19: scope 内に外部の依存宣言サイドカー（doc_links.yaml 等）を置いても、
    # scan はそれを一切読まず、フロントマターに書かれた依存のみをグラフに反映する。
    _write(tmp_path, "docs/design.md", _doc("design:d", "design"))
    _write(tmp_path, "docs/doc_links.yaml", "design:d:\n  - req:phantom\n")
    config = _config(scope={"include": ["docs/**/*.md"], "exclude": []})
    result = cli.scan_project(tmp_path, config)
    node = result.graph.nodes["design:d"]
    # サイドカー由来の依存（req:phantom）は取り込まれない。
    assert node.depends_on == ()
    assert not result.graph.has("req:phantom")


# ---------------------------------------------------------------------------
# EV-22: 壊れた入力・存在しない scope でもクラッシュしない
# ---------------------------------------------------------------------------


def test_scan_survives_malformed_frontmatter_without_crash(tmp_path) -> None:
    # 壊れた YAML（閉じられていない配列）はクラッシュせず missing_frontmatter 扱い。
    _write(tmp_path, "docs/broken.md", "---\ncodd: [unclosed\n---\n# body\n")
    result = cli.scan_project(tmp_path, _config())
    assert result.nodes == []
    assert result.missing_frontmatter == ["docs/broken.md"]
    assert cli.cmd_validate(tmp_path, _config()) == 0


def test_cmd_validate_survives_missing_root_directory(tmp_path) -> None:
    # 存在しない --root パスでもクラッシュせず、対象 0 件として正常終了する。
    missing_root = tmp_path / "does-not-exist"
    assert cli.cmd_validate(missing_root, _config()) == 0


# ---------------------------------------------------------------------------
# EV-23: graph.jsonl の書き込み失敗が既存グラフを破損させない（atomic write）
# ---------------------------------------------------------------------------


def test_write_graph_jsonl_failure_preserves_existing_graph(tmp_path, monkeypatch) -> None:
    out = tmp_path / ".claude/codd/graph.jsonl"
    out.parent.mkdir(parents=True)
    existing_content = '{"node_id": "design:existing"}\n'
    out.write_text(existing_content, encoding="utf-8")

    _write(tmp_path, "docs/design.md", _doc("design:d", "design"))
    result = cli.scan_project(tmp_path, _config())

    original_write_text = Path.write_text

    def _boom(self: Path, *args, **kwargs):
        if self.name.endswith(".tmp"):
            raise OSError("simulated write failure")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _boom)

    with pytest.raises(OSError):
        cli.write_graph_jsonl(result, out)

    # 書き込み失敗前の既存グラフがそのまま残る（半端な内容で壊れていない）。
    assert out.read_text(encoding="utf-8") == existing_content


# ---------------------------------------------------------------------------
# EV-13（should）: AI Orchestra 自身のドキュメントに対する dogfood validate
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_codd_validate_dogfoods_ai_orchestra_docs_with_zero_errors() -> None:
    # EV-13: AI Orchestra 自身のドキュメント群に対して validate を実行すると
    # error 0 で通る（ドッグフード）。warning（drift/orphan/missing_frontmatter 等）
    # は許容する。
    config_path = _REPO_ROOT / ".claude" / "config" / "codd" / "codd.yaml"
    if not config_path.exists():
        pytest.skip("codd config が未導入のためスキップ")
    config = cc.load_config(config_path)
    result = cli.scan_project(_REPO_ROOT, config)
    findings = cli.run_checks(result, config, _REPO_ROOT)
    errors = [f for f in findings if f.level == cc.LEVEL_ERROR]
    assert errors == [], "\n".join(f"{f.check}: {f.message}" for f in errors)


# ---------------------------------------------------------------------------
# EV-21: `orchex run codd codd -- <subcommand>` の後方互換サブプロセス起動
# ---------------------------------------------------------------------------

_ORCHESTRA_MANAGER = _REPO_ROOT / "scripts" / "orchestra-manager.py"

_MINIMAL_CODD_YAML = """\
enabled: true
scope:
  include: ["docs/**/*.md"]
  exclude: []
kinds: [requirement, design, adr, plan, rule, instruction]
relations: [derives_from, refines, implements, references, supersedes]
roots: [requirement, instruction]
graph_store:
  format: jsonl
  path: ".claude/codd/graph.jsonl"
checks:
  dangling: error
  duplicate: error
  cycle: error
  unknown: error
  missing_frontmatter: warning
  orphan: warning
  drift: warning
"""


def _run_orchex_codd(project: Path, *subcommand_args: str) -> subprocess.CompletedProcess[str]:
    """`orchex run codd codd -- <subcommand>` 相当のサブプロセス起動。"""
    import sys

    cmd = [
        sys.executable,
        str(_ORCHESTRA_MANAGER),
        "run",
        "codd",
        "codd",
        "--project",
        str(project),
        "--",
        *subcommand_args,
    ]
    env = {**os.environ, "AI_ORCHESTRA_DIR": str(_REPO_ROOT)}
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)


def test_orchex_run_codd_scan_and_validate_backward_compatible(tmp_path) -> None:
    # EV-21: scan / validate は `orchex run codd codd -- <subcommand>` として
    # サブプロセス起動・引数パースが後方互換に動作する。
    project = tmp_path / "project"
    _write(project, "docs/req.md", _doc("req:r", "requirement"))
    config_dir = project / ".claude" / "config" / "codd"
    config_dir.mkdir(parents=True)
    (config_dir / "codd.yaml").write_text(_MINIMAL_CODD_YAML, encoding="utf-8")

    scan_result = _run_orchex_codd(project, "scan")
    assert scan_result.returncode == 0, scan_result.stderr
    assert "[codd scan]" in scan_result.stdout
    assert (project / ".claude" / "codd" / "graph.jsonl").exists()

    validate_result = _run_orchex_codd(project, "validate")
    assert validate_result.returncode == 0, validate_result.stderr
    assert "[codd validate]" in validate_result.stdout
