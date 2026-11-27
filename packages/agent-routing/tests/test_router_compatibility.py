"""廃止済み CLI の設定を router が直接実行の経路へ解決することを検証する。"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

from tests.module_loader import REPO_ROOT, load_module

sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "agent-routing" / "hooks"))
route_config = load_module(
    "route_config_compatibility", "packages/agent-routing/hooks/route_config.py"
)
agent_router = load_module(
    "agent_router_compatibility", "packages/agent-routing/hooks/agent-router.py"
)


@pytest.mark.parametrize("legacy_tool", ["antigravity", "gemini"])
def test_legacy_tool_aliases_belong_to_direct_execution(legacy_tool: str) -> None:
    config = {
        legacy_tool: {"enabled": True},
        "agents": {"researcher": {"tool": legacy_tool}},
    }

    aliases = route_config.build_aliases(config)

    assert "task:researcher" in aliases["claude-direct"]
    assert set(aliases) == {"codex", "claude-direct", "auto"}
    assert route_config.get_agent_tool("researcher", config) == "claude-direct"


@pytest.mark.parametrize("legacy_tool", ["antigravity", "gemini"])
def test_legacy_tool_does_not_propose_a_cli_command(legacy_tool: str) -> None:
    config = {legacy_tool: {"enabled": True, "model": "obsolete-model"}}

    assert route_config.build_cli_suggestion(legacy_tool, "researcher", "調査", config) is None


@pytest.mark.parametrize("codex_enabled", [True, False])
def test_auto_aliases_offer_only_available_codex(codex_enabled: bool) -> None:
    config = {
        "codex": {"enabled": codex_enabled},
        "agents": {"general-purpose": {"tool": "auto"}},
    }

    aliases = route_config.build_aliases(config)

    expected = {"task:general-purpose"}
    if codex_enabled:
        expected.add("bash:codex")
    assert set(aliases["auto"]) == expected


@pytest.mark.parametrize("legacy_tool", ["antigravity", "gemini"])
def test_router_local_legacy_tool_still_suggests_researcher_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_tool: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    config_dir = tmp_path / ".claude" / "config" / "agent-routing"
    config_dir.mkdir(parents=True)
    (config_dir / "cli-tools.yaml").write_text(
        yaml.safe_dump(
            {"router": {"engine": "keyword"}, "agents": {"researcher": {"tool": "claude-direct"}}}
        ),
        encoding="utf-8",
    )
    (config_dir / "cli-tools.local.yaml").write_text(
        yaml.safe_dump(
            {
                legacy_tool: {"enabled": True},
                "agents": {"researcher": {"tool": legacy_tool}},
            }
        ),
        encoding="utf-8",
    )
    output = io.StringIO()
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps({"prompt": "最新のライブラリを調べてください", "cwd": str(tmp_path)})
        ),
    )
    monkeypatch.setattr(sys, "stdout", output)

    with pytest.raises(SystemExit) as exited:
        agent_router.main()

    assert exited.value.code == 0
    context = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
    routing_line = next(line for line in context.splitlines() if line.startswith("[Agent Routing]"))
    assert "`researcher` (tool: claude-direct)" in routing_line
    assert "agents.researcher.tool" in context
    assert "cli-tools.local.yaml" in context
    assert "remove" in context
    assert 'Task(subagent_type="researcher", prompt="...")' in context.splitlines()
    assert not any("CLI]" in line or "`agy" in line for line in context.splitlines())
