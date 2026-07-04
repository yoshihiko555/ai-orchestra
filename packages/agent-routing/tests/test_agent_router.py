"""agent-routing パッケージのルーティングロジックテスト。

テスト対象:
- route_config.detect_agent: プロンプトからエージェントを検出
- route_config.get_agent_tool: エージェントの tool を取得
- route_config.build_aliases: tool ごとの alias リストを構築
- route_config.build_cli_suggestion: CLI コマンド提案文字列を構築
"""

from __future__ import annotations

import sys

from tests.module_loader import REPO_ROOT, load_module

sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))
route_config = load_module("route_config", "packages/agent-routing/hooks/route_config.py")


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
    agent, trigger = route_config.detect_agent("please help with api design and endpoint naming")
    assert agent == "api-designer"
    assert trigger in {"api design", "endpoint"}


def test_detect_agent_detects_frontend_dev_from_english_prompt() -> None:
    agent, trigger = route_config.detect_agent("build a react component for the dashboard")
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
# detect_agent: 単語境界による誤検知回帰テスト
# ---------------------------------------------------------------------------


def test_detect_agent_does_not_misfire_frontend_dev_on_quick() -> None:
    """ "quick" の部分文字列 "ui" で frontend-dev が誤検知されないこと。"""
    agent, _ = route_config.detect_agent("Let's build a quick prototype")
    assert agent != "frontend-dev"


def test_detect_agent_does_not_misfire_tester_on_latest() -> None:
    """ "latest" の部分文字列 "test" で tester が誤検知されないこと。"""
    agent, _ = route_config.detect_agent("what is the latest release")
    assert agent != "tester"


def test_detect_agent_does_not_misfire_backend_go_dev() -> None:
    """ "go" は単語境界で判定されるが、辞書順で先に "plan" にマッチするため
    backend-go-dev が誤って選ばれないこと（planner が選ばれるのは正当）。
    """
    agent, _ = route_config.detect_agent("let's go over the plan")
    assert agent != "backend-go-dev"


def test_detect_agent_still_detects_legitimate_ascii_trigger() -> None:
    """単語境界化後も正当な ASCII トリガーは引き続き検出される。"""
    agent, trigger = route_config.detect_agent("write a test for this")
    assert agent == "tester"
    assert trigger == "test"


def test_detect_agent_still_detects_legitimate_mixed_ja_ascii_prompt() -> None:
    """日本語文中の ASCII トリガー（React / UI）も引き続き検出される。"""
    agent, trigger = route_config.detect_agent("React の UI を実装して")
    assert agent == "frontend-dev"
    assert trigger in {"React", "UI"}


def test_detect_agent_detects_ascii_trigger_adjacent_to_japanese() -> None:
    agent, _ = route_config.detect_agent("ReactのUIを実装して")
    assert agent == "frontend-dev"


def test_detect_agent_detects_dotted_ascii_trigger_adjacent_to_japanese() -> None:
    agent, _ = route_config.detect_agent("Next.jsで画面を作る")
    assert agent == "frontend-dev"


# ---------------------------------------------------------------------------
# is_cli_enabled（hook_common からの re-export、後方互換確認）
# ---------------------------------------------------------------------------


def test_is_cli_enabled_backward_compat_import() -> None:
    """route_config.is_cli_enabled は hook_common からの re-export として動作する。"""
    assert route_config.is_cli_enabled("codex", {"codex": {"enabled": True}}) is True
    assert route_config.is_cli_enabled("codex", {"codex": {"enabled": False}}) is False
    assert route_config.is_cli_enabled("codex", {}) is True


# ---------------------------------------------------------------------------
# get_agent_tool
# ---------------------------------------------------------------------------


def test_get_agent_tool_returns_configured_tool() -> None:
    config = {"agents": {"tester": {"tool": "codex", "sandbox": "workspace-write"}}}
    assert route_config.get_agent_tool("tester", config) == "codex"


def test_get_agent_tool_from_config_multiple() -> None:
    config = {
        "agents": {
            "architect": {"tool": "claude-direct"},
            "ai-dev": {"tool": "codex"},
        }
    }
    assert route_config.get_agent_tool("architect", config) == "claude-direct"
    assert route_config.get_agent_tool("ai-dev", config) == "codex"


def test_get_agent_tool_returns_claude_direct_for_missing_agent() -> None:
    config = {"agents": {}}
    assert route_config.get_agent_tool("unknown", config) == "claude-direct"


def test_get_agent_tool_returns_claude_direct_for_non_dict_config() -> None:
    config = {"agents": {"broken": "not-a-dict"}}
    assert route_config.get_agent_tool("broken", config) == "claude-direct"


def test_get_agent_tool_empty_config() -> None:
    assert route_config.get_agent_tool("architect", {}) == "claude-direct"


# ---------------------------------------------------------------------------
# build_aliases
# ---------------------------------------------------------------------------


def test_build_aliases_from_config() -> None:
    config = {
        "agents": {
            "architect": {"tool": "claude-direct"},
            "debugger": {"tool": "codex"},
            "researcher": {"tool": "antigravity"},
            "general-purpose": {"tool": "auto"},
        }
    }
    aliases = route_config.build_aliases(config)
    assert "task:architect" in aliases["claude-direct"]
    assert "task:debugger" in aliases["codex"]
    assert "task:researcher" in aliases["antigravity"]
    assert "task:general-purpose" in aliases["auto"]


def test_build_aliases_legacy_gemini_tool_value() -> None:
    """旧 tool 値 gemini は antigravity に読み替えられる。"""
    config = {"agents": {"researcher": {"tool": "gemini"}}}
    aliases = route_config.build_aliases(config)
    assert "task:researcher" in aliases["antigravity"]


def test_build_aliases_follows_config_change() -> None:
    """config 変更で aliases が自動追従。"""
    v1 = {"agents": {"architect": {"tool": "claude-direct"}}}
    v2 = {"agents": {"architect": {"tool": "codex"}}}
    assert "task:architect" in route_config.build_aliases(v1)["claude-direct"]
    assert "task:architect" in route_config.build_aliases(v2)["codex"]
    assert "task:architect" not in route_config.build_aliases(v2).get("claude-direct", [])


def test_build_aliases_base_aliases_present() -> None:
    """基本 aliases（bash:codex 等）は常に存在。"""
    aliases = route_config.build_aliases({})
    assert "bash:codex" in aliases["codex"]
    assert "bash:agy" in aliases["antigravity"]
    assert "bash:codex" in aliases["auto"]
    assert "bash:agy" in aliases["auto"]


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


def test_build_cli_suggestion_antigravity() -> None:
    config = {"antigravity": {"model": "gemini-3.1-pro-high"}}
    result = route_config.build_cli_suggestion("antigravity", "researcher", "調べて", config)
    assert result is not None
    assert "Antigravity CLI" in result
    assert "gemini-3.1-pro-high" in result
    assert "agy -p" in result


def test_build_cli_suggestion_antigravity_no_model() -> None:
    config = {"antigravity": {"model": ""}}
    result = route_config.build_cli_suggestion("antigravity", "researcher", "調べて", config)
    assert result is not None
    assert "--model" not in result


def test_build_cli_suggestion_claude_direct_returns_none() -> None:
    config = {}
    result = route_config.build_cli_suggestion("claude-direct", "planner", "計画", config)
    assert result is None


def test_build_cli_suggestion_antigravity_no_stdin_redirect() -> None:
    """EV-11: agy -p は非対話完結のため stdin リダイレクト不要。

    `< /dev/null` は含まれないこと（`2>/dev/null` は許容されるため
    "/dev/null" 全体ではなく "< /dev/null" を厳密に検証する）。
    """
    config = {"antigravity": {"model": "gemini-3.1-pro-high"}}
    result = route_config.build_cli_suggestion("antigravity", "researcher", "調べて", config)
    assert result is not None
    assert "< /dev/null" not in result


def test_build_cli_suggestion_codex_shows_analysis_sandbox_only() -> None:
    """EV-22: hook 提案は分析用（analysis）sandbox のみを表示する現状実装（仕様未文書化）の確認。

    implementation sandbox の値は提案文字列に含まれないこと。
    """
    config = {
        "codex": {
            "model": "gpt-5.3-codex",
            "sandbox": {"analysis": "dummy-analysis-mode", "implementation": "dummy-impl-mode"},
            "flags": "--full-auto",
        },
    }
    result = route_config.build_cli_suggestion("codex", "tester", "テスト", config)
    assert result is not None
    assert "dummy-analysis-mode" in result
    assert "dummy-impl-mode" not in result
