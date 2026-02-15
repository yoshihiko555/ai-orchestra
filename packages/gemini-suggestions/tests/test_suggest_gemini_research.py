from tests.module_loader import load_module

suggest_gemini_research = load_module(
    "suggest_gemini_research",
    "packages/gemini-suggestions/hooks/suggest-gemini-research.py",
)


def test_should_suggest_gemini_false_for_simple_lookup() -> None:
    should_suggest, reason = suggest_gemini_research.should_suggest_gemini(
        "check latest version and release notes"
    )
    assert not should_suggest
    assert reason == ""


def test_should_suggest_gemini_true_for_research_indicator_in_query() -> None:
    should_suggest, reason = suggest_gemini_research.should_suggest_gemini(
        "fastapi best practice for dependency injection"
    )
    assert should_suggest
    assert "best practice" in reason


def test_should_suggest_gemini_true_for_research_indicator_in_url() -> None:
    should_suggest, reason = suggest_gemini_research.should_suggest_gemini(
        "quick check", "https://docs.acme.test/tutorial/python"
    )
    assert should_suggest
    assert "tutorial" in reason


def test_should_suggest_gemini_true_for_long_query() -> None:
    query = "a" * 101
    should_suggest, reason = suggest_gemini_research.should_suggest_gemini(query)
    assert should_suggest
    assert reason == "Complex research query detected"


def test_should_suggest_gemini_false_when_no_condition_matches() -> None:
    should_suggest, reason = suggest_gemini_research.should_suggest_gemini(
        "what time is it now?"
    )
    assert not should_suggest
    assert reason == ""
