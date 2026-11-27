"""廃止済み CLI の設定を残したプロジェクトのルーティング互換を検証する。"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

from tests.module_loader import load_module

hook_common = load_module("hook_common", "packages/core/hooks/hook_common.py")
inject_mod = load_module(
    "inject_routing_compatibility", "packages/core/hooks/inject-shared-context.py"
)


def _assert_migration_warning(notes: list[str], legacy_key: str) -> None:
    warnings = [note for note in notes if legacy_key in note]
    assert warnings, notes
    assert any(
        "cli-tools.local.yaml" in note
        and re.search(r"remov|migrat|replace|削除|移行", note, re.IGNORECASE)
        for note in warnings
    ), notes
    assert all(len(note.splitlines()) == 1 for note in warnings)


@pytest.mark.parametrize("legacy_tool", ["antigravity", "gemini"])
@pytest.mark.parametrize("enabled", [None, True, False], ids=["no-section", "enabled", "disabled"])
def test_legacy_tool_resolves_direct_with_migration_warning(
    legacy_tool: str, enabled: bool | None
) -> None:
    config = {"agents": {"researcher": {"tool": legacy_tool}}}
    if enabled is not None:
        config[legacy_tool] = {"enabled": enabled}

    routing = hook_common.resolve_agent_routing("researcher", config)

    assert routing["tool"] == "claude-direct"
    assert set(routing) == {"agent", "tool", "notes"}
    assert hook_common.get_agent_tool("researcher", config) == "claude-direct"
    _assert_migration_warning(routing["notes"], "agents.researcher.tool")
    assert any(legacy_tool in note for note in routing["notes"])


@pytest.mark.parametrize("legacy_key", ["antigravity", "gemini"])
@pytest.mark.parametrize(
    "legacy_section",
    [
        {"enabled": True, "model": "unused-model\nforged-line", "flags": "unused-flags"},
        None,
        "obsolete-section",
    ],
    ids=["mapping", "null", "scalar"],
)
def test_legacy_section_is_ignored_and_warned_without_changing_codex(
    legacy_key: str, legacy_section: object
) -> None:
    config = {
        "codex": {"model": "retained-model", "flags": ""},
        "agents": {"reviewer": {"tool": "codex", "sandbox": "read-only"}},
        legacy_key: legacy_section,
    }

    routing = hook_common.resolve_agent_routing("reviewer", config)

    assert routing["tool"] == "codex"
    assert set(routing) == {"agent", "tool", "notes", "codex"}
    assert routing["codex"]["model"] == "retained-model"
    assert routing["codex"]["sandbox"] == "read-only"
    _assert_migration_warning(routing["notes"], legacy_key)
    assert not any("unused-" in note or "forged-line" in note for note in routing["notes"])


def _write_project_config(
    project: Path, monkeypatch: pytest.MonkeyPatch, base: dict, local: dict
) -> None:
    monkeypatch.setenv("HOME", str(project))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    config_dir = project / ".claude" / "config" / "agent-routing"
    config_dir.mkdir(parents=True)
    for name, config in (("cli-tools.yaml", base), ("cli-tools.local.yaml", local)):
        (config_dir / name).write_text(yaml.safe_dump(config), encoding="utf-8")


def _run_hook(payload: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", output)
    inject_mod.main()
    return json.loads(output.getvalue())["hookSpecificOutput"]


@pytest.mark.parametrize("legacy_tool", ["antigravity", "gemini"])
@pytest.mark.parametrize("event", ["SubagentStart", "Agent", "Task"])
def test_hook_local_legacy_settings_deliver_direct_routing_and_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_tool: str, event: str
) -> None:
    _write_project_config(
        tmp_path,
        monkeypatch,
        {"agents": {"researcher": {"tool": "claude-direct"}}},
        {
            legacy_tool: {"enabled": True, "model": "discarded-model", "flags": "--old-flag"},
            "agents": {"researcher": {"tool": legacy_tool}},
        },
    )
    payload: dict = {"cwd": str(tmp_path)}
    if event == "SubagentStart":
        payload.update(hook_event_name=event, agent_type="researcher")
    else:
        payload.update(
            hook_event_name="PreToolUse",
            tool_name=event,
            tool_input={"subagent_type": "researcher", "prompt": "資料を調査してください"},
        )

    output = _run_hook(payload, monkeypatch)

    assert set(output) == {"hookEventName", "additionalContext"}
    context = output["additionalContext"]
    value_lines = [line for line in context.splitlines() if line.startswith("- ")]
    fields = dict(line[2:].split(": ", 1) for line in value_lines)
    assert fields["agent"] == "researcher"
    assert fields["tool"] == "claude-direct"
    assert set(fields) == {"agent", "tool", "note"}
    notes = [line.removeprefix("- note: ") for line in value_lines if line.startswith("- note:")]
    _assert_migration_warning(notes, "agents.researcher.tool")
    _assert_migration_warning(notes, legacy_tool)
    assert "discarded-model" not in context
    assert "--old-flag" not in context
    if event == "SubagentStart":
        assert output["hookEventName"] == "SubagentStart"
        assert "[Resolved Routing]" in context.splitlines()
    else:
        assert output["hookEventName"] == "PreToolUse"
        assert context.startswith("[Subagent Context Preview]")


@pytest.mark.parametrize("codex_enabled", [True, False])
def test_hook_auto_delivers_only_available_codex_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codex_enabled: bool
) -> None:
    _write_project_config(
        tmp_path,
        monkeypatch,
        {"agents": {"general-purpose": {"tool": "auto"}}},
        {"codex": {"enabled": codex_enabled, "model": "chosen-model", "flags": ""}},
    )

    output = _run_hook(
        {
            "hook_event_name": "SubagentStart",
            "agent_type": "general-purpose",
            "cwd": str(tmp_path),
        },
        monkeypatch,
    )

    context = output["additionalContext"]
    fields = dict(line[2:].split(": ", 1) for line in context.splitlines() if line.startswith("- "))
    expected_fields = {"agent", "tool"}
    if codex_enabled:
        expected_fields.update(
            {
                "codex.model",
                "codex.sandbox.analysis",
                "codex.sandbox.implementation",
                "codex.flags",
                "codex.requires_sandbox_disable",
            }
        )
        assert fields["codex.model"] == "chosen-model"
        assert fields["codex.sandbox.analysis"] == "read-only"
        assert fields["codex.sandbox.implementation"] == "workspace-write"
    assert fields["tool"] == "auto"
    assert set(fields) == expected_fields
