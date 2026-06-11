from tests.module_loader import load_module

suggest_antigravity_research = load_module(
    "suggest_antigravity_research",
    "packages/antigravity-suggestions/hooks/suggest-antigravity-research.py",
)


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
