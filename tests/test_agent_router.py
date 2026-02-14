from tests.module_loader import load_module

agent_router = load_module("agent_router", "packages/route-audit/hooks/agent-router.py")


def test_detect_cli_tool_detects_codex_trigger() -> None:
    tool, trigger = agent_router.detect_cli_tool("設計相談をしたいです")
    assert tool == "codex"
    assert trigger == "設計相談"


def test_detect_cli_tool_detects_gemini_trigger() -> None:
    tool, trigger = agent_router.detect_cli_tool("最新ドキュメントを調べてください")
    assert tool == "gemini"
    assert trigger in {"調べて", "最新ドキュメント"}


def test_detect_cli_tool_prioritizes_codex_when_both_match() -> None:
    tool, trigger = agent_router.detect_cli_tool("設計相談しつつ最新ドキュメントも調べて")
    assert tool == "codex"
    assert trigger == "設計相談"


def test_detect_cli_tool_returns_none_when_no_match() -> None:
    tool, trigger = agent_router.detect_cli_tool("こんにちは")
    assert tool is None
    assert trigger == ""


def test_detect_agent_detects_tester() -> None:
    agent, trigger = agent_router.detect_agent("単体テストのカバレッジを上げたい")
    assert agent == "tester"
    assert trigger in {"テスト", "単体テスト", "カバレッジ"}


def test_detect_agent_detects_api_designer_from_english_prompt() -> None:
    agent, trigger = agent_router.detect_agent("please help with api design and endpoint naming")
    assert agent == "api-designer"
    assert trigger in {"api design", "endpoint"}


def test_detect_agent_returns_none_when_no_match() -> None:
    agent, trigger = agent_router.detect_agent("just saying hello")
    assert agent is None
    assert trigger == ""
