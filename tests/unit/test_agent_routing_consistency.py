"""cli-tools.yaml ↔ route_config.py ↔ agents/*.md の整合性テスト。

テスト観点:
- cli-tools.yaml の agents セクションに定義された全エージェントに .md ファイルが存在する
- route_config.AGENT_TRIGGERS が cli-tools.yaml の全エージェントを網羅している
- get_agent_tool が cli-tools.yaml の実データに対して正しい tool を返す
- build_aliases が全エージェントの tool 種別に合った alias を生成する
- エージェント .md のフォールバックデフォルトが cli-tools.yaml と矛盾しない
"""

from __future__ import annotations

import os
import re
import sys

import pytest

from tests.module_loader import REPO_ROOT, load_module

sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))
hook_common = load_module("hook_common", "packages/core/hooks/hook_common.py")
route_config = load_module("route_config", "packages/agent-routing/hooks/route_config.py")

# 実 config を読み込む
os.environ["AI_ORCHESTRA_DIR"] = str(REPO_ROOT)
_REAL_CONFIG = hook_common.load_package_config("agent-routing", "cli-tools.yaml", str(REPO_ROOT))
_AGENTS_IN_CONFIG = set(_REAL_CONFIG.get("agents", {}).keys())
_AGENTS_DIR = REPO_ROOT / "packages" / "agent-routing" / "agents"


# ---------------------------------------------------------------------------
# agents/*.md の存在チェック
# ---------------------------------------------------------------------------


class TestAgentMdFilesExist:
    """cli-tools.yaml に定義された全エージェントに .md ファイルがあるか。"""

    @pytest.mark.parametrize("agent_name", sorted(_AGENTS_IN_CONFIG))
    def test_agent_md_exists(self, agent_name: str) -> None:
        md_path = _AGENTS_DIR / f"{agent_name}.md"
        assert md_path.is_file(), f"packages/agent-routing/agents/{agent_name}.md が見つかりません"


class TestNoOrphanAgentMd:
    """agents/ に .md があるが cli-tools.yaml に未定義のエージェントがないか。"""

    def test_all_md_files_have_config_entry(self) -> None:
        md_files = {p.stem for p in _AGENTS_DIR.glob("*.md")}
        orphans = md_files - _AGENTS_IN_CONFIG
        assert not orphans, (
            f"cli-tools.yaml に未定義のエージェント .md (packages/agent-routing/agents/): {sorted(orphans)}"
        )


# ---------------------------------------------------------------------------
# AGENT_TRIGGERS 網羅性
# ---------------------------------------------------------------------------


class TestAgentTriggersCompleteness:
    """route_config.AGENT_TRIGGERS が cli-tools.yaml の全エージェントをカバーしているか。"""

    def test_all_config_agents_have_triggers(self) -> None:
        triggers_agents = set(route_config.AGENT_TRIGGERS.keys())
        # general-purpose は汎用エージェントでトリガー不要の場合がある
        config_agents = _AGENTS_IN_CONFIG - {"general-purpose"}
        missing = config_agents - triggers_agents
        assert not missing, f"AGENT_TRIGGERS に定義がないエージェント: {sorted(missing)}"

    def test_no_orphan_triggers(self) -> None:
        triggers_agents = set(route_config.AGENT_TRIGGERS.keys())
        orphans = triggers_agents - _AGENTS_IN_CONFIG
        assert not orphans, (
            f"cli-tools.yaml に未定義だが AGENT_TRIGGERS にあるエージェント: {sorted(orphans)}"
        )

    @pytest.mark.parametrize("agent_name", sorted(route_config.AGENT_TRIGGERS.keys()))
    def test_trigger_has_ja_and_en(self, agent_name: str) -> None:
        triggers = route_config.AGENT_TRIGGERS[agent_name]
        assert "ja" in triggers, f"{agent_name} に日本語トリガーがありません"
        assert "en" in triggers, f"{agent_name} に英語トリガーがありません"
        assert len(triggers["ja"]) >= 1, f"{agent_name} の日本語トリガーが空です"
        assert len(triggers["en"]) >= 1, f"{agent_name} の英語トリガーが空です"


# ---------------------------------------------------------------------------
# get_agent_tool: 実 config に対する動作
# ---------------------------------------------------------------------------

_VALID_TOOLS = {"codex", "antigravity", "claude-direct", "auto"}


class TestGetAgentToolWithRealConfig:
    @pytest.mark.parametrize("agent_name", sorted(_AGENTS_IN_CONFIG))
    def test_returns_valid_tool(self, agent_name: str) -> None:
        tool = route_config.get_agent_tool(agent_name, _REAL_CONFIG)
        assert tool in _VALID_TOOLS, f"{agent_name} の tool が不正: {tool}"

    def test_specific_tools_match_config(self) -> None:
        """cli-tools.yaml のカテゴリ分けが正しく反映されているか。"""
        agents = _REAL_CONFIG["agents"]
        # レビュー系は claude-direct
        for name in ["code-reviewer", "security-reviewer", "performance-reviewer"]:
            assert agents[name]["tool"] == "claude-direct"
        # 実装系は codex
        for name in ["tester", "debugger", "frontend-dev", "backend-python-dev"]:
            assert agents[name]["tool"] == "codex"
        # リサーチ系は antigravity
        assert agents["researcher"]["tool"] == "antigravity"


# ---------------------------------------------------------------------------
# build_aliases: 実 config に対する動作
# ---------------------------------------------------------------------------


class TestBuildAliasesWithRealConfig:
    def test_all_agents_appear_in_aliases(self) -> None:
        aliases = route_config.build_aliases(_REAL_CONFIG)
        all_task_aliases = set()
        for alias_list in aliases.values():
            all_task_aliases.update(a for a in alias_list if a.startswith("task:"))
        for agent_name in _AGENTS_IN_CONFIG:
            assert f"task:{agent_name}" in all_task_aliases, (
                f"build_aliases に task:{agent_name} がありません"
            )

    def test_codex_tool_has_bash_codex_alias(self) -> None:
        aliases = route_config.build_aliases(_REAL_CONFIG)
        assert "bash:codex" in aliases.get("codex", [])

    def test_antigravity_tool_has_bash_agy_alias(self) -> None:
        aliases = route_config.build_aliases(_REAL_CONFIG)
        assert "bash:agy" in aliases.get("antigravity", [])


# ---------------------------------------------------------------------------
# エージェント .md のフォールバックデフォルトと cli-tools.yaml の整合性
# ---------------------------------------------------------------------------

# 各 .md から「フォールバックデフォルト」のセクションを解析する
_FALLBACK_TOOL_RE = re.compile(r"^-\s*Tool:\s*(.+)", re.MULTILINE)


class TestAgentMdFallbackConsistency:
    """エージェント .md のフォールバックデフォルト Tool が cli-tools.yaml と矛盾しないか。"""

    @pytest.mark.parametrize("agent_name", sorted(_AGENTS_IN_CONFIG))
    def test_fallback_tool_matches_config(self, agent_name: str) -> None:
        md_path = _AGENTS_DIR / f"{agent_name}.md"
        if not md_path.is_file():
            pytest.skip(f"agents/{agent_name}.md not found")

        content = md_path.read_text(encoding="utf-8")
        match = _FALLBACK_TOOL_RE.search(content)
        if not match:
            pytest.skip(f"{agent_name}.md にフォールバックデフォルト記載なし")

        fallback_tool = match.group(1).strip().lower()
        config_tool = route_config.get_agent_tool(agent_name, _REAL_CONFIG)

        # auto の場合はどの fallback でも許容
        if config_tool == "auto":
            return

        assert fallback_tool == config_tool, (
            f"{agent_name}.md のフォールバック ({fallback_tool}) と "
            f"cli-tools.yaml の tool ({config_tool}) が不一致"
        )


# ---------------------------------------------------------------------------
# is_cli_enabled: enabled フラグの動作
# ---------------------------------------------------------------------------


def _make_config(**overrides: dict) -> dict:
    """テスト用の最小 config を構築する。"""
    base: dict = {
        "codex": {"enabled": True, "model": "gpt-5.5"},
        "antigravity": {"enabled": True, "model": "gemini-3.1-pro-high"},
        "agents": {
            "debugger": {"tool": "codex"},
            "researcher": {"tool": "antigravity"},
            "planner": {"tool": "claude-direct"},
            "ai-architect": {"tool": "auto"},
        },
    }
    for key, val in overrides.items():
        if isinstance(val, dict) and key in base and isinstance(base[key], dict):
            base[key] = {**base[key], **val}
        else:
            base[key] = val
    return base


class TestIsCliEnabled:
    """is_cli_enabled の enabled/disabled/未定義/セクション欠落ケース。"""

    def test_enabled_true(self) -> None:
        config = _make_config(codex={"enabled": True})
        assert route_config.is_cli_enabled("codex", config) is True

    def test_enabled_false(self) -> None:
        config = _make_config(codex={"enabled": False})
        assert route_config.is_cli_enabled("codex", config) is False

    def test_enabled_key_missing_defaults_true(self) -> None:
        """enabled キー未定義時は True（後方互換）。"""
        config = _make_config(codex={"model": "gpt-5.3-codex"})
        assert route_config.is_cli_enabled("codex", config) is True

    def test_section_missing_defaults_true(self) -> None:
        """CLI セクション自体が存在しない場合も True（後方互換）。"""
        assert route_config.is_cli_enabled("codex", {}) is True

    def test_antigravity_enabled_false(self) -> None:
        config = _make_config(antigravity={"enabled": False})
        assert route_config.is_cli_enabled("antigravity", config) is False

    def test_antigravity_enabled_true(self) -> None:
        config = _make_config(antigravity={"enabled": True})
        assert route_config.is_cli_enabled("antigravity", config) is True


class TestGetAgentToolFallback:
    """CLI 無効時に get_agent_tool が claude-direct にフォールバックするか。"""

    def test_codex_disabled_falls_back(self) -> None:
        config = _make_config(codex={"enabled": False})
        assert route_config.get_agent_tool("debugger", config) == "claude-direct"

    def test_codex_enabled_returns_codex(self) -> None:
        config = _make_config(codex={"enabled": True})
        assert route_config.get_agent_tool("debugger", config) == "codex"

    def test_antigravity_disabled_falls_back(self) -> None:
        config = _make_config(antigravity={"enabled": False})
        assert route_config.get_agent_tool("researcher", config) == "claude-direct"

    def test_antigravity_enabled_returns_antigravity(self) -> None:
        config = _make_config(antigravity={"enabled": True})
        assert route_config.get_agent_tool("researcher", config) == "antigravity"

    def test_claude_direct_unaffected(self) -> None:
        """claude-direct エージェントは CLI 無効の影響を受けない。"""
        config = _make_config(codex={"enabled": False}, antigravity={"enabled": False})
        assert route_config.get_agent_tool("planner", config) == "claude-direct"

    def test_auto_unaffected(self) -> None:
        """auto エージェントはフォールバックしない（サブエージェントが動的判断）。"""
        config = _make_config(codex={"enabled": False}, antigravity={"enabled": False})
        assert route_config.get_agent_tool("ai-architect", config) == "auto"


class TestBuildCliSuggestionDisabled:
    """CLI 無効時に build_cli_suggestion が None を返すか。"""

    def test_codex_disabled_returns_none(self) -> None:
        config = _make_config(codex={"enabled": False})
        result = route_config.build_cli_suggestion("codex", "debugger", "debug", config)
        assert result is None

    def test_codex_enabled_returns_string(self) -> None:
        config = _make_config(codex={"enabled": True})
        result = route_config.build_cli_suggestion("codex", "debugger", "debug", config)
        assert result is not None
        assert "Codex" in result
        assert "< /dev/null" in result

    def test_antigravity_disabled_returns_none(self) -> None:
        config = _make_config(antigravity={"enabled": False})
        result = route_config.build_cli_suggestion("antigravity", "researcher", "research", config)
        assert result is None

    def test_antigravity_enabled_returns_string(self) -> None:
        config = _make_config(antigravity={"enabled": True})
        result = route_config.build_cli_suggestion("antigravity", "researcher", "research", config)
        assert result is not None
        assert "Antigravity" in result
        assert "agy -p" in result
        assert "--model gemini-3.1-pro-high" in result

    def test_antigravity_model_not_in_allowlist_warns(self) -> None:
        """allowlist 未掲載 model は WARN を提案文字列に含める。"""
        config = _make_config(
            antigravity={
                "enabled": True,
                "model": "totally-bogus",
                "model_allowlist": ["gemini-3.1-pro-high"],
            }
        )
        result = route_config.build_cli_suggestion("antigravity", "researcher", "research", config)
        assert result is not None
        assert "[WARN]" in result

    def test_antigravity_model_in_allowlist_no_warn(self) -> None:
        config = _make_config(
            antigravity={
                "enabled": True,
                "model": "gemini-3.1-pro-high",
                "model_allowlist": ["gemini-3.1-pro-high"],
            }
        )
        result = route_config.build_cli_suggestion("antigravity", "researcher", "research", config)
        assert result is not None
        assert "[WARN]" not in result

    def test_claude_direct_always_none(self) -> None:
        config = _make_config()
        result = route_config.build_cli_suggestion("claude-direct", "planner", "plan", config)
        assert result is None


class TestBuildAliasesDisabled:
    """CLI 無効時のエイリアス構築確認。"""

    def test_codex_disabled_no_bash_codex(self) -> None:
        config = _make_config(codex={"enabled": False})
        aliases = route_config.build_aliases(config)
        assert "bash:codex" not in aliases.get("codex", [])

    def test_codex_disabled_agents_move_to_claude_direct(self) -> None:
        config = _make_config(codex={"enabled": False})
        aliases = route_config.build_aliases(config)
        assert "task:debugger" in aliases.get("claude-direct", [])

    def test_antigravity_disabled_no_bash_agy(self) -> None:
        config = _make_config(antigravity={"enabled": False})
        aliases = route_config.build_aliases(config)
        assert "bash:agy" not in aliases.get("antigravity", [])

    def test_antigravity_disabled_agents_move_to_claude_direct(self) -> None:
        config = _make_config(antigravity={"enabled": False})
        aliases = route_config.build_aliases(config)
        assert "task:researcher" in aliases.get("claude-direct", [])

    def test_both_disabled_auto_has_no_bash(self) -> None:
        config = _make_config(codex={"enabled": False}, antigravity={"enabled": False})
        aliases = route_config.build_aliases(config)
        auto_aliases = aliases.get("auto", [])
        assert "bash:codex" not in auto_aliases
        assert "bash:agy" not in auto_aliases

    def test_both_enabled_auto_has_both_bash(self) -> None:
        config = _make_config()
        aliases = route_config.build_aliases(config)
        auto_aliases = aliases.get("auto", [])
        assert "bash:codex" in auto_aliases
        assert "bash:agy" in auto_aliases


# ---------------------------------------------------------------------------
# 旧 gemini 設定（横展開先 .local.yaml 残存分）の後方互換
# ---------------------------------------------------------------------------


def _make_legacy_config(**overrides: dict) -> dict:
    """旧 gemini キー形式の config（移行前の .local.yaml 相当）を構築する。"""
    base: dict = {
        "codex": {"enabled": True, "model": "gpt-5.5"},
        "antigravity": {"enabled": True, "model": "gemini-3.1-pro-high"},
        "gemini": {"enabled": True, "model": "gemini-3.1-pro-preview"},
        "agents": {
            "researcher": {"tool": "gemini"},
        },
    }
    for key, val in overrides.items():
        if isinstance(val, dict) and key in base and isinstance(base[key], dict):
            base[key] = {**base[key], **val}
        else:
            base[key] = val
    return base


class TestLegacyGeminiCompat:
    """旧 gemini 系設定が antigravity に読み替えられるか（エイリアス 3 形態）。"""

    def test_normalize_rewrites_agent_tool(self) -> None:
        """形態 1: agents.*.tool: gemini → antigravity。"""
        config = hook_common.normalize_cli_tools_config(_make_legacy_config())
        assert config["agents"]["researcher"]["tool"] == "antigravity"

    def test_normalize_applies_legacy_disabled(self) -> None:
        """形態 2: gemini.enabled: false → antigravity.enabled: false。"""
        config = hook_common.normalize_cli_tools_config(
            _make_legacy_config(gemini={"enabled": False})
        )
        assert config["antigravity"]["enabled"] is False

    def test_normalize_ignores_legacy_model(self) -> None:
        """形態 3: gemini.model（Gemini CLI 固有値）は引き継がない。"""
        config = hook_common.normalize_cli_tools_config(_make_legacy_config())
        assert config["antigravity"]["model"] == "gemini-3.1-pro-high"

    def test_normalize_does_not_mutate_input(self) -> None:
        original = _make_legacy_config()
        hook_common.normalize_cli_tools_config(original)
        assert original["agents"]["researcher"]["tool"] == "gemini"

    def test_get_agent_tool_accepts_legacy_tool_value(self) -> None:
        """正規化前の config を直接渡しても gemini → antigravity に読み替える。"""
        config = _make_legacy_config()
        assert route_config.get_agent_tool("researcher", config) == "antigravity"

    def test_build_cli_suggestion_accepts_legacy_tool_value(self) -> None:
        config = _make_legacy_config()
        result = route_config.build_cli_suggestion("gemini", "researcher", "research", config)
        assert result is not None
        assert "agy -p" in result

    def test_build_aliases_accepts_legacy_tool_value(self) -> None:
        config = _make_legacy_config()
        aliases = route_config.build_aliases(config)
        assert "task:researcher" in aliases.get("antigravity", [])
