"""agent-routing パッケージのルーティングロジックテスト。

テスト対象:
- route_config.detect_agent: プロンプトからエージェントを検出
- route_config.get_agent_tool: エージェントの tool を取得
- route_config.build_cli_suggestion: CLI コマンド提案文字列を構築
"""

from __future__ import annotations

import sys

from tests.module_loader import REPO_ROOT, load_module

sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))
route_config = load_module(
    "route_config", "packages/agent-routing/hooks/route_config.py"
)


# ---------------------------------------------------------------------------
# detect_agent: 日本語トリガー
# ---------------------------------------------------------------------------


def test_detect_agent_detects_tester() -> None:
    agent, trigger = route_config.detect_agent("単体テストのカバレッジを上げたい")
    assert agent == "tester"
    assert trigger in {"テスト", "単体テスト", "カバレッジ"}


def test_detect_agent_detects_debugger() -> None:
    agent, trigger = route_config.detect_agent("このエラーをデバッグしたい")
    assert agent == "debugger"
    assert trigger in {"デバッグ", "エラー"}


def test_detect_agent_detects_researcher() -> None:
    agent, trigger = route_config.detect_agent("最新のライブラリについて調べてください")
    assert agent == "researcher"
    assert trigger == "調べて"


def test_detect_agent_detects_architect() -> None:
    agent, trigger = route_config.detect_agent("システムのアーキテクチャを設計して")
    assert agent == "architect"
    assert trigger == "アーキテクチャ"


def test_detect_agent_detects_planner() -> None:
    agent, trigger = route_config.detect_agent("タスクを計画してほしい")
    assert agent == "planner"
    assert trigger == "計画"


# ---------------------------------------------------------------------------
# detect_agent: 英語トリガー
# ---------------------------------------------------------------------------


def test_detect_agent_detects_api_designer_from_english_prompt() -> None:
    agent, trigger = route_config.detect_agent(
        "please help with api design and endpoint naming"
    )
    assert agent == "api-designer"
    assert trigger in {"api design", "endpoint"}


def test_detect_agent_detects_frontend_dev_from_english_prompt() -> None:
    agent, trigger = route_config.detect_agent(
        "build a react component for the dashboard"
    )
    assert agent == "frontend-dev"
    assert trigger.lower() == "react"


def test_detect_agent_detects_security_reviewer() -> None:
    agent, trigger = route_config.detect_agent("run a security review on this code")
    assert agent == "security-reviewer"
    assert trigger == "security review"


# ---------------------------------------------------------------------------
# detect_agent: 該当なし
# ---------------------------------------------------------------------------


def test_detect_agent_returns_none_when_no_match() -> None:
    agent, trigger = route_config.detect_agent("just saying hello")
    assert agent is None
    assert trigger == ""


def test_detect_agent_returns_none_for_empty_prompt() -> None:
    agent, trigger = route_config.detect_agent("")
    assert agent is None
    assert trigger == ""


# ---------------------------------------------------------------------------
# get_agent_tool
# ---------------------------------------------------------------------------


def test_get_agent_tool_returns_configured_tool() -> None:
    config = {"agents": {"tester": {"tool": "codex", "sandbox": "workspace-write"}}}
    assert route_config.get_agent_tool("tester", config) == "codex"


def test_get_agent_tool_returns_claude_direct_for_missing_agent() -> None:
    config = {"agents": {}}
    assert route_config.get_agent_tool("unknown", config) == "claude-direct"


def test_get_agent_tool_returns_claude_direct_for_non_dict_config() -> None:
    config = {"agents": {"broken": "not-a-dict"}}
    assert route_config.get_agent_tool("broken", config) == "claude-direct"


# ---------------------------------------------------------------------------
# build_cli_suggestion
# ---------------------------------------------------------------------------


def test_build_cli_suggestion_codex() -> None:
    config = {
        "codex": {
            "model": "gpt-5.3-codex",
            "sandbox": {"analysis": "read-only"},
            "flags": "--full-auto",
        },
    }
    result = route_config.build_cli_suggestion("codex", "tester", "テスト", config)
    assert result is not None
    assert "Codex CLI" in result
    assert "gpt-5.3-codex" in result
    assert "read-only" in result
    assert "--full-auto" in result


def test_build_cli_suggestion_gemini() -> None:
    config = {"gemini": {"model": "gemini-2.5-pro"}}
    result = route_config.build_cli_suggestion("gemini", "researcher", "調べて", config)
    assert result is not None
    assert "Gemini CLI" in result
    assert "gemini-2.5-pro" in result
    assert "-p" in result


def test_build_cli_suggestion_gemini_no_model() -> None:
    config = {"gemini": {"model": ""}}
    result = route_config.build_cli_suggestion("gemini", "researcher", "調べて", config)
    assert result is not None
    assert "-m " not in result


def test_build_cli_suggestion_claude_direct_returns_none() -> None:
    config = {}
    result = route_config.build_cli_suggestion(
        "claude-direct", "planner", "計画", config
    )
    assert result is None
