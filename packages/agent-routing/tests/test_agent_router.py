"""agent-routing パッケージのユニットテスト。"""

from __future__ import annotations

import importlib.util
import os

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_hooks_dir = os.path.join(_tests_dir, "..", "hooks")

# route_config.py を importlib で読み込み
_config_spec = importlib.util.spec_from_file_location(
    "route_config",
    os.path.join(_hooks_dir, "route_config.py"),
)
assert _config_spec and _config_spec.loader
_config_mod = importlib.util.module_from_spec(_config_spec)
_config_spec.loader.exec_module(_config_mod)

detect_agent = _config_mod.detect_agent  # type: ignore[attr-defined]
get_agent_tool = _config_mod.get_agent_tool  # type: ignore[attr-defined]
build_aliases = _config_mod.build_aliases  # type: ignore[attr-defined]
build_cli_suggestion = _config_mod.build_cli_suggestion  # type: ignore[attr-defined]


# ========== detect_agent テスト ==========


class TestDetectAgent:
    """detect_agent() のテスト。"""

    def test_detect_agent_detects_tester(self) -> None:
        agent, trigger = detect_agent("テストを書いてください")
        assert agent == "tester"
        assert trigger == "テスト"

    def test_detect_agent_detects_api_designer_from_english_prompt(self) -> None:
        agent, trigger = detect_agent("Please design the API endpoints for user management")
        assert agent == "api-designer"
        assert "endpoint" in trigger

    def test_detect_agent_returns_none_when_no_match(self) -> None:
        agent, trigger = detect_agent("こんにちは")
        assert agent is None
        assert trigger == ""


# ========== get_agent_tool テスト ==========


class TestGetAgentTool:
    """get_agent_tool() のテスト。"""

    def test_get_agent_tool_from_config(self) -> None:
        config = {
            "agents": {
                "architect": {"tool": "claude-direct"},
                "ai-dev": {"tool": "codex"},
            }
        }
        assert get_agent_tool("architect", config) == "claude-direct"
        assert get_agent_tool("ai-dev", config) == "codex"

    def test_get_agent_tool_unknown_returns_default(self) -> None:
        config = {"agents": {"architect": {"tool": "claude-direct"}}}
        assert get_agent_tool("unknown", config) == "claude-direct"

    def test_get_agent_tool_empty_config(self) -> None:
        assert get_agent_tool("architect", {}) == "claude-direct"


# ========== build_aliases テスト ==========


class TestBuildAliases:
    """build_aliases() のテスト。"""

    def test_build_aliases_from_config(self) -> None:
        config = {
            "agents": {
                "architect": {"tool": "claude-direct"},
                "debugger": {"tool": "codex"},
                "researcher": {"tool": "gemini"},
                "general-purpose": {"tool": "auto"},
            }
        }
        aliases = build_aliases(config)
        assert "task:architect" in aliases["claude-direct"]
        assert "task:debugger" in aliases["codex"]
        assert "task:researcher" in aliases["gemini"]
        assert "task:general-purpose" in aliases["auto"]

    def test_build_aliases_follows_config_change(self) -> None:
        """config 変更で aliases が自動追従。"""
        v1 = {"agents": {"architect": {"tool": "claude-direct"}}}
        v2 = {"agents": {"architect": {"tool": "codex"}}}
        assert "task:architect" in build_aliases(v1)["claude-direct"]
        assert "task:architect" in build_aliases(v2)["codex"]
        assert "task:architect" not in build_aliases(v2).get("claude-direct", [])

    def test_build_aliases_base_aliases_present(self) -> None:
        """基本 aliases（bash:codex 等）は常に存在。"""
        aliases = build_aliases({})
        assert "bash:codex" in aliases["codex"]
        assert "bash:gemini" in aliases["gemini"]
        assert "bash:codex" in aliases["auto"]
        assert "bash:gemini" in aliases["auto"]


# ========== build_cli_suggestion テスト ==========


class TestBuildCliSuggestion:
    """build_cli_suggestion() のテスト。"""

    def test_codex_suggestion(self) -> None:
        config = {
            "codex": {"model": "gpt-5.3-codex", "sandbox": {"analysis": "read-only"}, "flags": "--full-auto"}
        }
        result = build_cli_suggestion("codex", "debugger", "デバッグ", config)
        assert result is not None
        assert "Codex CLI" in result
        assert "gpt-5.3-codex" in result

    def test_gemini_suggestion(self) -> None:
        config = {"gemini": {"model": "gemini-2.5-pro"}}
        result = build_cli_suggestion("gemini", "researcher", "調査", config)
        assert result is not None
        assert "Gemini CLI" in result
        assert "gemini-2.5-pro" in result

    def test_claude_direct_returns_none(self) -> None:
        result = build_cli_suggestion("claude-direct", "architect", "設計", {})
        assert result is None
