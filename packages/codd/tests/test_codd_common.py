"""codd_common（parser / グラフモデル / config ローダー）の unit test。"""

from __future__ import annotations

from pathlib import Path

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
    assert set(cycles[0]) == {"a", "b"}


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
    assert set(cycles[0]) == {"a"}


def test_find_cycles_three_node_cycle_reported_once() -> None:
    graph = codd.build_graph([_node("a", ["b"]), _node("b", ["c"]), _node("c", ["a"])])
    cycles = graph.find_cycles()
    assert len(cycles) == 1
    assert set(cycles[0]) == {"a", "b", "c"}


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
