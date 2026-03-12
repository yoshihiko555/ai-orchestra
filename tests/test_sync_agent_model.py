"""sync-orchestra.py の model 関連ユーティリティのテスト。

既存パターン（module_loader.load_module + tmp_path + yaml.dump）に従う。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.module_loader import load_module

sync_mod = load_module("sync_orchestra", "scripts/sync-orchestra.py")
resolve_agent_model = sync_mod.resolve_agent_model
_patch_agent_model = sync_mod._patch_agent_model
_load_cli_tools_config = sync_mod._load_cli_tools_config
_deep_merge = sync_mod._deep_merge


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
