import sys

from tests.module_loader import REPO_ROOT, load_module

suggest_antigravity_research = load_module(
    "suggest_antigravity_research",
    "packages/antigravity-suggestions/hooks/suggest-antigravity-research.py",
)


def test_imports_without_agent_routing_hooks_on_path() -> None:
    """agent-routing の hooks ディレクトリが sys.path に無くても import できる。

    manifest.json の depends は ["core"] のみであり、is_cli_enabled は
    hook_common（core）から直接 import するため agent-routing への暗黙依存が
    無いことを保証する回帰テスト。
    """
    routing_hooks_dir = str(REPO_ROOT / "packages" / "agent-routing" / "hooks")
    removed_paths = [p for p in sys.path if p == routing_hooks_dir]
    for path in removed_paths:
        sys.path.remove(path)
    sys.modules.pop("route_config", None)
    try:
        assert routing_hooks_dir not in sys.path
        module = load_module(
            "suggest_antigravity_research_no_routing_dep",
            "packages/antigravity-suggestions/hooks/suggest-antigravity-research.py",
        )
        assert hasattr(module, "is_cli_enabled")
    finally:
        sys.path.extend(removed_paths)


def test_should_suggest_antigravity_false_for_simple_lookup() -> None:
    should_suggest, reason = suggest_antigravity_research.should_suggest_antigravity(
        "check latest version and release notes"
    )
    assert not should_suggest
    assert reason == ""


def test_should_suggest_antigravity_true_for_research_indicator_in_query() -> None:
    should_suggest, reason = suggest_antigravity_research.should_suggest_antigravity(
        "fastapi best practice for dependency injection"
    )
    assert should_suggest
    assert "best practice" in reason


def test_should_suggest_antigravity_true_for_research_indicator_in_url() -> None:
    should_suggest, reason = suggest_antigravity_research.should_suggest_antigravity(
        "quick check", "https://docs.acme.test/tutorial/python"
    )
    assert should_suggest
    assert "tutorial" in reason


def test_should_suggest_antigravity_true_for_long_query() -> None:
    query = "a" * 101
    should_suggest, reason = suggest_antigravity_research.should_suggest_antigravity(query)
    assert should_suggest
    assert reason == "Complex research query detected"


def test_should_suggest_antigravity_false_when_no_condition_matches() -> None:
    should_suggest, reason = suggest_antigravity_research.should_suggest_antigravity(
        "what time is it now?"
    )
    assert not should_suggest
    assert reason == ""


def test_should_suggest_antigravity_true_when_research_indicator_outranks_simple_lookup() -> None:
    """研究シグナル（migration/pattern）は "version" 系の単純確認より優先される。"""
    should_suggest, _ = suggest_antigravity_research.should_suggest_antigravity(
        "compare api versioning migration patterns"
    )
    assert should_suggest is True


def test_should_suggest_antigravity_false_for_simple_version_lookup() -> None:
    """研究シグナルを含まない単純な version 確認は引き続き抑制される。"""
    should_suggest, reason = suggest_antigravity_research.should_suggest_antigravity(
        "what is the latest version of react"
    )
    assert should_suggest is False
    assert reason == ""
