"""codd_common（parser / グラフモデル / config ローダー）の unit test。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.module_loader import load_module

codd = load_module("codd_common", "packages/codd/lib/codd_common.py")


# ---------------------------------------------------------------------------
# frontmatter parser
# ---------------------------------------------------------------------------


def test_extract_frontmatter_block_reads_leading_block_only() -> None:
    text = "---\ncodd:\n  node_id: a\n---\n\n# 本文\n\n```\n---\n無視される\n---\n```\n"
    block = codd.extract_frontmatter_block(text)
    assert block == "codd:\n  node_id: a"


def test_extract_frontmatter_block_none_when_not_leading() -> None:
    text = "# 見出し\n\n---\ncodd:\n  node_id: a\n---\n"
    assert codd.extract_frontmatter_block(text) is None


def test_extract_frontmatter_block_none_when_unterminated() -> None:
    text = "---\ncodd:\n  node_id: a\n"
    assert codd.extract_frontmatter_block(text) is None


def test_parse_codd_frontmatter_returns_codd_dict() -> None:
    text = "---\ncodd:\n  node_id: design:x\n  kind: design\n---\n# body\n"
    result = codd.parse_codd_frontmatter(text)
    assert result == {"node_id": "design:x", "kind": "design"}


def test_parse_codd_frontmatter_none_without_codd_key() -> None:
    text = "---\ntitle: just a doc\n---\n"
    assert codd.parse_codd_frontmatter(text) is None


def test_parse_codd_frontmatter_none_on_invalid_yaml() -> None:
    text = "---\ncodd: [unclosed\n---\n"
    assert codd.parse_codd_frontmatter(text) is None


# ---------------------------------------------------------------------------
# node model
# ---------------------------------------------------------------------------


def test_build_node_parses_dependencies() -> None:
    block = {
        "node_id": "design:x",
        "kind": "design",
        "status": "draft",
        "depends_on": [{"id": "req:y", "relation": "derives_from"}],
        "owner": "team",
    }
    node = codd.build_node(block, "docs/x.md")
    assert node.node_id == "design:x"
    assert node.kind == "design"
    assert node.owner == "team"
    assert node.depends_on == (codd.Dependency(id="req:y", relation="derives_from"),)


def test_build_node_tolerates_missing_fields() -> None:
    node = codd.build_node({"node_id": "plan:p"}, ".claude/Plans.md")
    assert node.kind == ""
    assert node.status == ""
    assert node.depends_on == ()
    assert node.owner is None


def test_build_node_normalizes_yaml_null_to_empty() -> None:
    # YAML の null（明示）が文字列 "None" にならず空文字へ正規化される。
    block = {
        "node_id": None,
        "kind": None,
        "status": None,
        "depends_on": [{"id": None, "relation": None}],
        "owner": None,
    }
    node = codd.build_node(block, "docs/x.md")
    assert node.node_id == ""
    assert node.kind == ""
    assert node.status == ""
    assert node.depends_on == (codd.Dependency(id="", relation=""),)
    assert node.owner is None


# ---------------------------------------------------------------------------
# graph model
# ---------------------------------------------------------------------------


def _node(node_id: str, deps: list[str], kind: str = "design") -> codd.CoddNode:
    dependencies = tuple(codd.Dependency(id=d, relation="derives_from") for d in deps)
    return codd.CoddNode(
        node_id=node_id,
        kind=kind,
        status="draft",
        depends_on=dependencies,
        owner=None,
        path=f"docs/{node_id}.md",
    )


def test_build_graph_indexes_nodes() -> None:
    graph = codd.build_graph([_node("a", []), _node("b", ["a"])])
    assert graph.has("a")
    assert graph.has("b")
    assert graph.incoming_count("a") == 1
    assert graph.incoming_count("b") == 0


def test_graph_records_duplicate_node_ids() -> None:
    graph = codd.build_graph([_node("a", []), _node("a", [])])
    assert "a" in graph.duplicate_paths
    assert len(graph.duplicate_paths["a"]) == 2


def test_find_cycles_detects_simple_cycle() -> None:
    graph = codd.build_graph([_node("a", ["b"]), _node("b", ["a"])])
    cycles = graph.find_cycles()
    assert len(cycles) == 1
    # 「始点 == 終点」で閉じる契約を検証する。
    assert cycles[0][0] == cycles[0][-1]
    assert set(cycles[0][:-1]) == {"a", "b"}


def test_find_cycles_empty_for_dag() -> None:
    graph = codd.build_graph([_node("a", []), _node("b", ["a"]), _node("c", ["b"])])
    assert graph.find_cycles() == []


def test_find_cycles_ignores_dangling_edges() -> None:
    graph = codd.build_graph([_node("a", ["missing"])])
    assert graph.find_cycles() == []


def test_find_cycles_detects_self_loop() -> None:
    graph = codd.build_graph([_node("a", ["a"])])
    cycles = graph.find_cycles()
    assert len(cycles) == 1
    assert cycles[0] == ["a", "a"]


def test_find_cycles_three_node_cycle_reported_once() -> None:
    graph = codd.build_graph([_node("a", ["b"]), _node("b", ["c"]), _node("c", ["a"])])
    cycles = graph.find_cycles()
    assert len(cycles) == 1
    assert cycles[0][0] == cycles[0][-1]
    assert set(cycles[0][:-1]) == {"a", "b", "c"}


def test_extract_frontmatter_block_none_for_empty_text() -> None:
    assert codd.extract_frontmatter_block("") is None
    assert codd.extract_frontmatter_block("\n") is None
    assert codd.parse_codd_frontmatter("") is None


# ---------------------------------------------------------------------------
# config loader
# ---------------------------------------------------------------------------


def test_valid_statuses_by_kind() -> None:
    assert "accepted" in codd.valid_statuses("adr")
    assert "draft" in codd.valid_statuses("design")
    assert codd.valid_statuses("unknown") == []


# ---------------------------------------------------------------------------
# node_id 形式（EV-12: `<kind>:<file-slug>` 形式検証）
# ---------------------------------------------------------------------------


def test_node_id_prefix_extracts_prefix_before_colon() -> None:
    assert codd.node_id_prefix("design:architecture") == "design"
    assert codd.node_id_prefix("req:coherence-guardrail") == "req"


def test_node_id_prefix_none_without_colon() -> None:
    # コロン無し node_id は `<kind>:<file-slug>` 形式を満たさない。
    assert codd.node_id_prefix("designarchitecture") is None


def test_node_id_prefix_none_when_prefix_or_slug_empty() -> None:
    assert codd.node_id_prefix(":architecture") is None  # プレフィックス空
    assert codd.node_id_prefix("design:") is None  # スラッグ空


def test_node_id_prefix_none_with_extra_separator() -> None:
    # EV-12: コロンが複数個ある node_id（余分なセパレータ）は
    # `<kind>:<file-slug>` 形式（コロンちょうど1個）を満たさず None になる。
    assert codd.node_id_prefix("design:foo:bar") is None
    assert codd.node_id_prefix("adr:ADR-20260624-010:extra") is None


def test_node_id_prefix_still_accepts_valid_ids() -> None:
    # EV-12: 既存の正当な node_id（file-slug 内にコロンを含まない）は
    # 複数セパレータ拒否の追加後も引き続き受理される。
    assert codd.node_id_prefix("req:feature-list") == "req"
    assert codd.node_id_prefix("adr:ADR-20260624-010") == "adr"
    assert codd.node_id_prefix("design:codd-coherence-layer") == "design"


def test_node_id_prefix_by_kind_matches_design_table() -> None:
    # 設計 4.3 の表: requirement のみ "req" に略記、他は kind 名と同一。
    # code/test は Issue #98（コード⇔ドキュメントのトレーサビリティ）で追加された kind。
    assert codd.NODE_ID_PREFIX_BY_KIND == {
        "requirement": "req",
        "design": "design",
        "adr": "adr",
        "plan": "plan",
        "rule": "rule",
        "instruction": "instruction",
        "code": "code",
        "test": "test",
    }


def test_deep_merge_overrides_and_preserves() -> None:
    base = {"a": 1, "nested": {"x": 1, "y": 2}}
    override = {"nested": {"y": 9}, "b": 3}
    merged = codd.deep_merge(base, override)
    assert merged == {"a": 1, "nested": {"x": 1, "y": 9}, "b": 3}


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_load_config_reads_base(tmp_path) -> None:
    cfg_path = tmp_path / "codd.yaml"
    _write(
        cfg_path,
        "enabled: true\n"
        "scope:\n  include:\n    - 'docs/**/*.md'\n  exclude: []\n"
        "kinds: [design, adr]\n"
        "relations: [derives_from]\n"
        "roots: [requirement]\n"
        "graph_store:\n  format: jsonl\n  path: '.claude/codd/graph.jsonl'\n"
        "checks:\n  dangling: error\n",
    )
    config = codd.load_config(cfg_path)
    assert config.enabled is True
    assert config.include == ["docs/**/*.md"]
    assert config.kinds == ["design", "adr"]
    assert config.graph_path == ".claude/codd/graph.jsonl"
    assert config.checks["dangling"] == "error"


def test_load_config_applies_local_override(tmp_path) -> None:
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "enabled: true\nkinds: [design]\nchecks:\n  drift: warning\n")
    _write(tmp_path / "codd.local.yaml", "enabled: false\nchecks:\n  drift: off\n")
    config = codd.load_config(cfg_path)
    assert config.enabled is False
    assert config.checks["drift"] == "off"
    # local で触れていない kinds は base が残る
    assert config.kinds == ["design"]


def test_load_config_defaults_when_missing(tmp_path) -> None:
    config = codd.load_config(tmp_path / "absent.yaml")
    assert config.graph_format == codd.DEFAULT_GRAPH_FORMAT
    assert config.graph_path == codd.DEFAULT_GRAPH_PATH
    assert config.include == []


def test_load_config_raises_value_error_on_invalid_yaml(tmp_path) -> None:

    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "checks:\n  dangling: [unclosed\n")
    with pytest.raises(ValueError, match="Invalid CODD config YAML"):
        codd.load_config(cfg_path)


def test_load_config_raises_value_error_on_typo_check_level(tmp_path) -> None:
    # checks の値に typo（例: eror）があると Finding の level がどの集計にも
    # 一致せず validate が無音の成功になる（CI ゲートのサイレント無効化）ため、
    # config ロード時点で明確なエラーにする。
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "checks:\n  dangling: eror\n")
    with pytest.raises(ValueError, match="Invalid check level"):
        codd.load_config(cfg_path)


def test_normalize_check_level_accepts_known_values() -> None:
    assert codd.normalize_check_level("error") == codd.LEVEL_ERROR
    assert codd.normalize_check_level("Warning") == codd.LEVEL_WARNING
    assert codd.normalize_check_level("  ERROR  ") == codd.LEVEL_ERROR
    assert codd.normalize_check_level("off") == codd.LEVEL_OFF
    assert codd.normalize_check_level(False) == codd.LEVEL_OFF  # YAML 1.1 bare off


def test_normalize_check_level_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="Invalid check level"):
        codd.normalize_check_level("eror")
    with pytest.raises(ValueError, match="Invalid check level"):
        codd.normalize_check_level("critical")


def test_load_config_accepts_all_known_check_levels(tmp_path) -> None:
    cfg_path = tmp_path / "codd.yaml"
    _write(
        cfg_path,
        "checks:\n  dangling: error\n  drift: warning\n  orphan: off\n  cycle: off\n",
    )
    config = codd.load_config(cfg_path)
    assert config.checks["dangling"] == codd.LEVEL_ERROR
    assert config.checks["drift"] == codd.LEVEL_WARNING
    assert config.checks["orphan"] == codd.LEVEL_OFF
    # bare `off`（YAML 1.1 boolean False）も同様に off へ揃う
    assert config.checks["cycle"] == codd.LEVEL_OFF


# ---------------------------------------------------------------------------
# inline_confidence の正規化（Issue #98 レビュー対応）
# ---------------------------------------------------------------------------


def test_load_config_inline_confidence_defaults_when_absent(tmp_path) -> None:
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "enabled: true\n")
    config = codd.load_config(cfg_path)
    assert config.inline_confidence == codd.DEFAULT_INLINE_CONFIDENCE


def test_load_config_inline_confidence_clamps_negative_value(tmp_path) -> None:
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "inline_confidence: -0.1\n")
    config = codd.load_config(cfg_path)
    assert config.inline_confidence == 0.0


def test_load_config_inline_confidence_clamps_value_above_one(tmp_path) -> None:
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "inline_confidence: 1.5\n")
    config = codd.load_config(cfg_path)
    assert config.inline_confidence == 1.0


def test_load_config_inline_confidence_falls_back_on_nan(tmp_path) -> None:
    # YAML の `.nan` は非有限値。エッジ重み計算（relation 重み × confidence）が
    # NaN で壊れ、JSONL 出力（json.dumps の非標準 NaN リテラル）も壊すため、
    # 既定値へフォールバックする。
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "inline_confidence: .nan\n")
    config = codd.load_config(cfg_path)
    assert config.inline_confidence == codd.DEFAULT_INLINE_CONFIDENCE


def test_load_config_inline_confidence_falls_back_on_non_numeric(tmp_path) -> None:
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "inline_confidence: not-a-number\n")
    config = codd.load_config(cfg_path)
    assert config.inline_confidence == codd.DEFAULT_INLINE_CONFIDENCE


def test_load_config_inline_confidence_falls_back_on_bool(tmp_path) -> None:
    # bool は int のサブクラスのため float(False) == 0.0 が例外なく通ってしまい、
    # `inline_confidence: false` が全エッジ重みゼロ（一斉 Gray 化）に化ける
    # 危険がある。bool は不正値として既定値へフォールバックする（Issue #98 レビュー対応）。
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "inline_confidence: false\n")
    config = codd.load_config(cfg_path)
    assert config.inline_confidence == codd.DEFAULT_INLINE_CONFIDENCE


def test_load_config_inline_confidence_true_falls_back_to_default(tmp_path) -> None:
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "inline_confidence: true\n")
    config = codd.load_config(cfg_path)
    assert config.inline_confidence == codd.DEFAULT_INLINE_CONFIDENCE


def test_as_confidence_rejects_bool() -> None:
    # depends_on.confidence も同じ bool 混入リスクがあるため、既定 1.0 へフォールバックする。
    assert codd._as_confidence(False) == 1.0
    assert codd._as_confidence(True) == 1.0


def test_impact_config_from_dict_rejects_bool_numeric_fields() -> None:
    # ImpactConfig.from_dict の数値フィールドに bool を渡すと TypeError で拒否する
    # （int()/float() が bool を黙って受理するのを防ぐ。Issue #98 レビュー対応）。
    for field in (
        "decay",
        "max_hops",
        "green_threshold",
        "amber_threshold",
        "strong_relation_min",
        "corroboration_min_origins",
        "evidence_bonus",
    ):
        with pytest.raises(TypeError):
            codd.ImpactConfig.from_dict({field: True})


def test_impact_config_from_dict_relation_weights_bool_falls_back_to_default() -> None:
    # relation_weights は既存の try/except で不正値を無視する設計のため、bool は
    # 該当 relation の既定重みを維持する（クラッシュではなくフォールバック）。
    config = codd.ImpactConfig.from_dict({"relation_weights": {"references": False}})
    assert config.relation_weights["references"] == codd.DEFAULT_RELATION_WEIGHTS["references"]


def test_impact_config_from_dict_rejects_non_finite_int_fields_as_value_error() -> None:
    # `impact.max_hops: .inf`（YAML の `float("inf")`）を素朴に `int()` へ渡すと
    # `OverflowError`（ValueError のサブクラスではない）を送出し、`main()` の
    # `except (TypeError, ValueError)` を素通りして未整形のトレースバックになる
    # （P1 レビュー対応: codd_common.py:420）。ValueError として整形された
    # 設定エラーになるべき。
    for field in ("max_hops", "corroboration_min_origins"):
        with pytest.raises(ValueError):
            codd.ImpactConfig.from_dict({field: float("inf")})
        with pytest.raises(ValueError):
            codd.ImpactConfig.from_dict({field: float("nan")})


def test_impact_config_from_dict_rejects_non_mapping_relation_weights() -> None:
    # `impact.relation_weights: []`（マッピング以外）は `.items()` で
    # `AttributeError` になり未整形のトレースバックになっていた
    # （P1 レビュー対応: codd_common.py:420）。ValueError として整形される。
    with pytest.raises(ValueError, match="relation_weights"):
        codd.ImpactConfig.from_dict({"relation_weights": ["references"]})


def test_load_config_rejects_non_mapping_impact_and_checks(tmp_path) -> None:
    # `impact: []` / `checks: []`（マッピング以外）は `.get()` / `.items()` で
    # `AttributeError` になり未整形のトレースバックになっていた
    # （P1 レビュー対応: codd_common.py:420）。ValueError として整形される。
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "impact:\n  - a\n  - b\n")
    with pytest.raises(ValueError, match="impact"):
        codd.load_config(cfg_path)

    cfg_path2 = tmp_path / "codd2.yaml"
    _write(cfg_path2, "checks:\n  - a\n  - b\n")
    with pytest.raises(ValueError, match="checks"):
        codd.load_config(cfg_path2)


# ---------------------------------------------------------------------------
# scope/code_scope の glob リスト正規化（Issue #98 レビュー対応）
# ---------------------------------------------------------------------------


def test_load_config_code_scope_include_accepts_single_string_as_list(tmp_path) -> None:
    # YAML でリスト記法（`- `）を忘れて単一文字列を書いた場合、素朴な list(str) だと
    # 1 文字ずつイテレートされ glob として無意味になる。単要素リストとして扱う。
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "code_scope:\n  include: 'src/**/*.py'\n  exclude: []\n")
    config = codd.load_config(cfg_path)
    assert config.code_include == ["src/**/*.py"]


def test_load_config_scope_include_accepts_single_string_as_list(tmp_path) -> None:
    # doc scope（`scope.include`）でも同じ正規化を適用し、コード側と扱いを一貫させる。
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "scope:\n  include: 'docs/**/*.md'\n  exclude: []\n")
    config = codd.load_config(cfg_path)
    assert config.include == ["docs/**/*.md"]


def test_load_config_code_scope_include_rejects_non_string_non_list(tmp_path) -> None:
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "code_scope:\n  include: 42\n")
    with pytest.raises(ValueError, match="code_scope.include"):
        codd.load_config(cfg_path)


def test_load_config_code_scope_include_rejects_list_with_non_string_items(tmp_path) -> None:
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "code_scope:\n  include:\n    - 'src/**/*.py'\n    - 3\n")
    with pytest.raises(ValueError, match="code_scope.include"):
        codd.load_config(cfg_path)


def test_load_config_scope_include_empty_string_means_no_targets(tmp_path) -> None:
    # `scope.include: ""` で「対象なし」を表す既存設定との後方互換（P1 レビュー対応）。
    # 単一文字列を単要素リスト化する変換を空文字列にも適用すると `[""]` になり、
    # 後続の `Path.glob("")` が ValueError で CLI をトレースバック終了させてしまう。
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "scope:\n  include: ''\n  exclude: ''\n")
    config = codd.load_config(cfg_path)
    assert config.include == []
    assert config.exclude == []


def test_load_config_code_scope_include_empty_string_means_no_targets(tmp_path) -> None:
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "code_scope:\n  include: ''\n  exclude: ''\n")
    config = codd.load_config(cfg_path)
    assert config.code_include == []
    assert config.code_exclude == []


def test_load_config_code_scope_include_list_with_empty_string_element_is_dropped(
    tmp_path,
) -> None:
    # `code_scope.include: ["", "src/**/*.py"]` のようにリスト**内**の空文字列要素も
    # 単独の空文字列と同じ「対象なし」の意味で扱い、除去する。除去せず `[""]` の
    # まま `Path.glob("")` に渡すと `ValueError: Unacceptable pattern: ''` になる
    # （Issue #98 レビュー対応: codd_common.py:665）。
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "code_scope:\n  include:\n    - ''\n    - 'src/**/*.py'\n")
    config = codd.load_config(cfg_path)
    assert config.code_include == ["src/**/*.py"]


def test_load_config_scope_exclude_list_with_only_empty_string_element_is_empty(
    tmp_path,
) -> None:
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "scope:\n  include: 'docs/**/*.md'\n  exclude:\n    - ''\n")
    config = codd.load_config(cfg_path)
    assert config.exclude == []


def test_load_config_code_scope_include_absent_defaults_to_empty_list(tmp_path) -> None:
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "enabled: true\n")
    config = codd.load_config(cfg_path)
    assert config.code_include == []
    assert config.code_exclude == []


def test_load_config_rejects_non_mapping_code_scope(tmp_path) -> None:
    # `code_scope: oops`（文字列）は `code_scope.get("include")` で AttributeError に
    # なり main() の (TypeError, ValueError) ハンドラを素通りしていた。mapping 以外は
    # ValueError として整形すべき（Issue #98 レビュー対応）。
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "code_scope: oops\n")
    with pytest.raises(ValueError, match="code_scope"):
        codd.load_config(cfg_path)


def test_load_config_rejects_non_mapping_scope(tmp_path) -> None:
    # doc scope（`scope`）でも同種問題が起きうるため同じ検証を適用する
    # （Issue #98 レビュー対応）。
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "scope:\n  - a\n  - b\n")
    with pytest.raises(ValueError, match="scope"):
        codd.load_config(cfg_path)


def test_load_config_rejects_non_mapping_graph_store(tmp_path) -> None:
    cfg_path = tmp_path / "codd.yaml"
    _write(cfg_path, "graph_store: oops\n")
    with pytest.raises(ValueError, match="graph_store"):
        codd.load_config(cfg_path)


def test_build_node_clamps_out_of_range_confidence() -> None:
    block = {
        "node_id": "design:x",
        "kind": "design",
        "status": "draft",
        "depends_on": [
            {"id": "req:a", "relation": "derives_from", "confidence": -0.1},
            {"id": "req:b", "relation": "derives_from", "confidence": 2.0},
        ],
    }
    node = codd.build_node(block, "docs/x.md")
    assert node.depends_on[0].confidence == 0.0
    assert node.depends_on[1].confidence == 1.0
