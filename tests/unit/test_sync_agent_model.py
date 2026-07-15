"""sync-orchestra.py の model 関連ユーティリティのテスト。

既存パターン（module_loader.load_module + tmp_path + yaml.dump）に従う。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.module_loader import load_module

sync_mod = load_module("agent_model_patch", "scripts/lib/agent_model_patch.py")
resolve_agent_model = sync_mod.resolve_agent_model
_patch_agent_model = sync_mod.patch_agent_model
_load_cli_tools_config = sync_mod.load_cli_tools_config
_deep_merge = sync_mod._deep_merge
patch_all_agents = sync_mod.patch_all_agents
patch_all_agents_paths = sync_mod.patch_all_agents_paths


FRONTMATTER_TEMPLATE = """\
---
name: planner
description: Task decomposition agent
tools: Read, Glob, Grep
model: {model}
---

You are a planning specialist.
"""


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data), encoding="utf-8")


class TestResolveAgentModel:
    def test_per_agent_model_returned(self) -> None:
        config = {
            "agents": {"planner": {"model": "opus"}},
            "subagent": {"default_model": "sonnet"},
        }
        assert resolve_agent_model("planner", config) == "opus"

    def test_null_model_falls_back_to_default(self) -> None:
        config = {
            "agents": {"planner": {"model": None}},
            "subagent": {"default_model": "sonnet"},
        }
        assert resolve_agent_model("planner", config) == "sonnet"

    def test_empty_string_model_falls_back_to_default(self) -> None:
        config = {
            "agents": {"planner": {"model": ""}},
            "subagent": {"default_model": "sonnet"},
        }
        assert resolve_agent_model("planner", config) == "sonnet"

    def test_agent_not_in_config_falls_back_to_default(self) -> None:
        config = {
            "agents": {"planner": {"model": "opus"}},
            "subagent": {"default_model": "sonnet"},
        }
        assert resolve_agent_model("unknown", config) == "sonnet"

    def test_no_default_model_returns_none(self) -> None:
        config = {"agents": {"planner": {"model": None}}}
        assert resolve_agent_model("planner", config) is None

    def test_empty_config_returns_none(self) -> None:
        assert resolve_agent_model("planner", {}) is None


class TestPatchAgentModel:
    def test_replaces_model_in_frontmatter(self, tmp_path: Path) -> None:
        agent_file = tmp_path / "planner.md"
        agent_file.write_text(FRONTMATTER_TEMPLATE.format(model="sonnet"), encoding="utf-8")

        changed = _patch_agent_model(agent_file, "opus")

        assert changed is True
        content = agent_file.read_text(encoding="utf-8")
        assert "model: opus" in content
        assert "model: sonnet" not in content

    def test_idempotent_same_model(self, tmp_path: Path) -> None:
        agent_file = tmp_path / "planner.md"
        agent_file.write_text(FRONTMATTER_TEMPLATE.format(model="opus"), encoding="utf-8")

        changed = _patch_agent_model(agent_file, "opus")

        assert changed is False

    def test_no_frontmatter(self, tmp_path: Path) -> None:
        agent_file = tmp_path / "planner.md"
        agent_file.write_text("You are a planning specialist.\n", encoding="utf-8")

        changed = _patch_agent_model(agent_file, "opus")

        assert changed is False

    def test_model_not_in_frontmatter(self, tmp_path: Path) -> None:
        agent_file = tmp_path / "planner.md"
        agent_file.write_text(
            """\
---
name: planner
description: Task decomposition agent
tools: Read, Glob, Grep
---

You are a planning specialist.
""",
            encoding="utf-8",
        )

        changed = _patch_agent_model(agent_file, "opus")

        assert changed is False

    def test_body_model_not_replaced(self, tmp_path: Path) -> None:
        agent_file = tmp_path / "planner.md"
        agent_file.write_text(
            """\
---
name: planner
description: Task decomposition agent
tools: Read, Glob, Grep
model: sonnet
---

You are a planning specialist.
model: something
""",
            encoding="utf-8",
        )

        changed = _patch_agent_model(agent_file, "opus")

        assert changed is True
        content = agent_file.read_text(encoding="utf-8")
        assert "model: opus" in content
        assert "model: something" in content


class TestLoadCliToolsConfig:
    def test_base_only(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        config_dir = project_dir / ".claude" / "config" / "agent-routing"
        _write_yaml(config_dir / "cli-tools.yaml", {"codex": {"model": "gpt-5"}})

        result = _load_cli_tools_config(project_dir)

        assert result == {"codex": {"model": "gpt-5"}}

    def test_base_plus_local_merged(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        config_dir = project_dir / ".claude" / "config" / "agent-routing"
        _write_yaml(config_dir / "cli-tools.yaml", {"codex": {"model": "gpt-5", "enabled": True}})
        _write_yaml(config_dir / "cli-tools.local.yaml", {"codex": {"model": "o3-pro"}})

        result = _load_cli_tools_config(project_dir)

        assert result == {"codex": {"model": "o3-pro", "enabled": True}}

    def test_neither_exists(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"

        result = _load_cli_tools_config(project_dir)

        assert result == {}


class TestDeepMerge:
    def test_simple_override(self) -> None:
        assert _deep_merge({"a": 1, "b": 2}, {"b": 99}) == {"a": 1, "b": 99}

    def test_nested_merge(self) -> None:
        assert _deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"x": 99}}) == {"a": {"x": 99, "y": 2}}

    def test_missing_key_preserved(self) -> None:
        assert _deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


def _write_agent_md(path: Path, model: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FRONTMATTER_TEMPLATE.format(model=model), encoding="utf-8")


class TestPatchAllAgentsAllowlist:
    """patch_all_agents の managed_agent_stems allowlist のテスト。"""

    def _setup_project(self, tmp_path: Path) -> Path:
        project_dir = tmp_path / "project"
        config_dir = project_dir / ".claude" / "config" / "agent-routing"
        _write_yaml(config_dir / "cli-tools.yaml", {"subagent": {"default_model": "opus"}})
        return project_dir

    def test_allowlist_protects_unmanaged_agent(self, tmp_path: Path) -> None:
        """allowlist 外のユーザー独自エージェント .md は変更されない。"""
        project_dir = self._setup_project(tmp_path)
        known = project_dir / ".claude" / "agents" / "known.md"
        custom = project_dir / ".claude" / "agents" / "user-custom.md"
        _write_agent_md(known, "sonnet")
        _write_agent_md(custom, "sonnet")

        patched_count = patch_all_agents(project_dir, managed_agent_stems={"known"})

        assert patched_count == 1
        assert "model: opus" in known.read_text(encoding="utf-8")
        assert "model: sonnet" in custom.read_text(encoding="utf-8")

    def test_none_allowlist_patches_all(self, tmp_path: Path) -> None:
        """managed_agent_stems が None の場合は全件パッチする（後方互換）。"""
        project_dir = self._setup_project(tmp_path)
        known = project_dir / ".claude" / "agents" / "known.md"
        custom = project_dir / ".claude" / "agents" / "user-custom.md"
        _write_agent_md(known, "sonnet")
        _write_agent_md(custom, "sonnet")

        patched_count = patch_all_agents(project_dir, None)

        assert patched_count == 2
        assert "model: opus" in known.read_text(encoding="utf-8")
        assert "model: opus" in custom.read_text(encoding="utf-8")

    def test_default_argument_patches_all(self, tmp_path: Path) -> None:
        """managed_agent_stems 省略時は全件パッチする（後方互換）。"""
        project_dir = self._setup_project(tmp_path)
        agent = project_dir / ".claude" / "agents" / "known.md"
        _write_agent_md(agent, "sonnet")

        patched_count = patch_all_agents(project_dir)

        assert patched_count == 1
        assert "model: opus" in agent.read_text(encoding="utf-8")

    def test_empty_allowlist_patches_nothing(self, tmp_path: Path) -> None:
        """空集合の allowlist は全ファイルをスキップする。"""
        project_dir = self._setup_project(tmp_path)
        agent = project_dir / ".claude" / "agents" / "known.md"
        _write_agent_md(agent, "sonnet")

        patched_count = patch_all_agents(project_dir, managed_agent_stems=set())

        assert patched_count == 0
        assert "model: sonnet" in agent.read_text(encoding="utf-8")


class TestPatchAllAgentsPaths:
    """patch_all_agents_paths のテスト（PR #244: file_hashes 台帳更新のための実パス取得）。"""

    def _setup_project(self, tmp_path: Path) -> Path:
        project_dir = tmp_path / "project"
        config_dir = project_dir / ".claude" / "config" / "agent-routing"
        _write_yaml(config_dir / "cli-tools.yaml", {"subagent": {"default_model": "opus"}})
        return project_dir

    def test_returns_actually_patched_paths(self, tmp_path: Path) -> None:
        """パッチが実際に適用されたファイルのみをパスのリストで返す。"""
        project_dir = self._setup_project(tmp_path)
        patched_target = project_dir / ".claude" / "agents" / "known.md"
        already_opus = project_dir / ".claude" / "agents" / "already-opus.md"
        _write_agent_md(patched_target, "sonnet")
        _write_agent_md(already_opus, "opus")

        result = patch_all_agents_paths(project_dir)

        assert result == [patched_target]

    def test_count_matches_patch_all_agents(self, tmp_path: Path) -> None:
        """patch_all_agents() の件数と patch_all_agents_paths() の要素数が一致する。"""
        project_dir = self._setup_project(tmp_path)
        _write_agent_md(project_dir / ".claude" / "agents" / "a.md", "sonnet")
        _write_agent_md(project_dir / ".claude" / "agents" / "b.md", "sonnet")

        count = patch_all_agents(project_dir)
        # 2回目呼び出し用に再度パッチ前の状態へ戻す
        _write_agent_md(project_dir / ".claude" / "agents" / "a.md", "sonnet")
        _write_agent_md(project_dir / ".claude" / "agents" / "b.md", "sonnet")
        paths = patch_all_agents_paths(project_dir)

        assert count == 2
        assert len(paths) == 2

    def test_empty_when_agents_dir_missing(self, tmp_path: Path) -> None:
        """.claude/agents ディレクトリが無ければ空リストを返す。"""
        project_dir = tmp_path / "project"

        assert patch_all_agents_paths(project_dir) == []
