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

_VALID_TOOLS = {"codex", "gemini", "claude-direct", "auto"}


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
        # リサーチ系は gemini
        assert agents["researcher"]["tool"] == "gemini"


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

    def test_gemini_tool_has_bash_gemini_alias(self) -> None:
        aliases = route_config.build_aliases(_REAL_CONFIG)
        assert "bash:gemini" in aliases.get("gemini", [])


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
