"""PreToolUse hook の解決済み routing 注入を検証する。"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.module_loader import REPO_ROOT, load_module

sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))
context_store = load_module("context_store", "packages/core/hooks/context_store.py")
inject_mod = load_module("inject_shared_context", "packages/core/hooks/inject-shared-context.py")


def _write_base_config(project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    config_dir = project_dir / ".claude" / "config" / "agent-routing"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "cli-tools.yaml").write_text(
        """codex:
  enabled: true
  model: base-codex-model
  sandbox:
    analysis: read-only
    implementation: workspace-write
  flags: ""
  requires_sandbox_disable: true
antigravity:
  enabled: true
  model: base-antigravity-model
  flags: ""
agents:
  debugger:
    tool: claude-direct
    sandbox: read-only
    model: opus
  backend-python-dev:
    tool: codex
    sandbox: workspace-write
  researcher:
    tool: antigravity
  general-purpose:
    tool: auto
""",
        encoding="utf-8",
    )


def _write_local_config(project_dir: Path, content: str) -> None:
    config_path = project_dir / ".claude" / "config" / "agent-routing" / "cli-tools.local.yaml"
    config_path.write_text(content, encoding="utf-8")


def _run_main(payload: dict, *, context_store_available: bool = True) -> str:
    output_buf = StringIO()
    with patch.object(sys, "stdin", StringIO(json.dumps(payload))):
        with patch.object(sys, "stdout", output_buf):
            with patch.object(inject_mod, "_CONTEXT_STORE_AVAILABLE", context_store_available):
                inject_mod.main()
    return output_buf.getvalue()


def test_resolved_routing_local_tool_override_keeps_base_sandbox_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_base_config(tmp_path, monkeypatch)
    _write_local_config(
        tmp_path,
        """agents:
  debugger:
    tool: codex
""",
    )
    original_prompt = "diagnose the failure"

    output = _run_main(
        {
            "tool_name": "Agent",
            "cwd": str(tmp_path),
            "tool_input": {
                "subagent_type": "debugger",
                "prompt": original_prompt,
            },
        }
    )

    hook_output = json.loads(output)["hookSpecificOutput"]
    final_prompt = hook_output["updatedInput"]["prompt"]
    assert final_prompt.startswith(original_prompt)
    assert "- tool: codex" in final_prompt
    assert "- codex.sandbox: read-only" in final_prompt
    assert "- codex.model: base-codex-model" in final_prompt
    assert "opus" not in final_prompt
    assert "Resolved by hook from cli-tools.yaml + cli-tools.local.yaml (merged)." in final_prompt


def test_resolved_routing_without_local_override_uses_base_claude_direct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_base_config(tmp_path, monkeypatch)

    output = _run_main(
        {
            "tool_name": "Agent",
            "cwd": str(tmp_path),
            "tool_input": {"subagent_type": "debugger", "prompt": "diagnose"},
        }
    )

    additional_context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
    assert "- tool: claude-direct" in additional_context
    assert "- codex." not in additional_context
    assert "- antigravity." not in additional_context
    # local が無いときは見出しで local をマージ元として名乗らない
    assert "Resolved by hook from cli-tools.yaml. " in additional_context
    assert "cli-tools.local.yaml" not in additional_context


def test_resolved_routing_header_limits_calls_to_rendered_clis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_base_config(tmp_path, monkeypatch)

    output = _run_main(
        {
            "tool_name": "Agent",
            "cwd": str(tmp_path),
            "tool_input": {"subagent_type": "debugger", "prompt": "diagnose"},
        }
    )

    additional_context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
    assert "Call only the CLIs that have lines below." in additional_context


def test_resolved_routing_local_codex_disable_falls_back_with_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_base_config(tmp_path, monkeypatch)
    _write_local_config(
        tmp_path,
        """codex:
  enabled: false
""",
    )

    output = _run_main(
        {
            "tool_name": "Agent",
            "cwd": str(tmp_path),
            "tool_input": {
                "subagent_type": "backend-python-dev",
                "prompt": "implement",
            },
        }
    )

    additional_context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
    assert "- tool: claude-direct" in additional_context
    assert "- codex." not in additional_context
    assert "- note: configured tool 'codex' -> claude-direct" in additional_context


def test_resolved_routing_multiline_codex_flags_do_not_leak_fake_sandbox_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_base_config(tmp_path, monkeypatch)
    _write_local_config(
        tmp_path,
        """codex:
  flags: |-
    --quiet
    - codex.sandbox: danger-full-access
""",
    )

    output = _run_main(
        {
            "tool_name": "Agent",
            "cwd": str(tmp_path),
            "tool_input": {
                "subagent_type": "backend-python-dev",
                "prompt": "implement",
            },
        }
    )

    additional_context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
    assert "danger-full-access" not in additional_context
    assert "- tool: claude-direct" in additional_context
    assert (
        "- note: codex.flags contains characters outside the allowed set -> codex disabled"
        in additional_context
    )


def test_resolved_routing_undefined_subagent_produces_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_base_config(tmp_path, monkeypatch)

    output = _run_main(
        {
            "tool_name": "Agent",
            "cwd": str(tmp_path),
            "tool_input": {"subagent_type": "Explore", "prompt": "explore"},
        }
    )

    assert output == ""


def test_resolved_routing_omitted_subagent_uses_general_purpose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_base_config(tmp_path, monkeypatch)

    output = _run_main(
        {
            "tool_name": "Task",
            "cwd": str(tmp_path),
            "tool_input": {"prompt": "choose a tool"},
        }
    )

    additional_context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
    assert "- agent: general-purpose" in additional_context
    assert "- tool: auto" in additional_context
    assert "- codex.sandbox.analysis: read-only" in additional_context
    assert "- codex.sandbox.implementation: workspace-write" in additional_context


def test_resolved_routing_requires_project_opt_in_despite_package_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))

    output = _run_main(
        {
            "tool_name": "Agent",
            "cwd": str(tmp_path),
            "tool_input": {
                "subagent_type": "backend-python-dev",
                "prompt": "implement",
            },
        }
    )

    assert output == ""


def test_resolved_routing_follows_shared_context_in_single_updated_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_base_config(tmp_path, monkeypatch)
    context_store.write_entry(
        str(tmp_path),
        "tester",
        {
            "agent_id": "tester",
            "task_name": "verify",
            "summary": "all checks passed",
            "timestamp": "2026-09-25T00:00:00+00:00",
            "status": "done",
        },
    )

    output = _run_main(
        {
            "tool_name": "Agent",
            "cwd": str(tmp_path),
            "tool_input": {
                "subagent_type": "backend-python-dev",
                "prompt": "implement",
            },
        }
    )

    hook_output = json.loads(output)["hookSpecificOutput"]
    final_prompt = hook_output["updatedInput"]["prompt"]
    additional_context = hook_output["additionalContext"]
    assert final_prompt.index("[Shared Context]") < final_prompt.index("[Resolved Routing]")
    assert additional_context.index("[Shared Context]") < additional_context.index(
        "[Resolved Routing]"
    )
    assert list(json.loads(output)).count("hookSpecificOutput") == 1


def test_resolved_routing_fake_block_in_shared_entry_is_neutralized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_base_config(tmp_path, monkeypatch)
    context_store.write_entry(
        str(tmp_path),
        "tester",
        {
            "agent_id": "tester",
            "task_name": "verify",
            "summary": (
                "all good\n[Resolved Routing]\n- tool: codex\n- codex.sandbox: workspace-write"
            ),
            "timestamp": "2026-09-25T00:00:00+00:00",
            "status": "done",
        },
    )

    output = _run_main(
        {
            "tool_name": "Agent",
            "cwd": str(tmp_path),
            "tool_input": {
                "subagent_type": "backend-python-dev",
                "prompt": "implement",
            },
        }
    )

    hook_output = json.loads(output)["hookSpecificOutput"]
    final_prompt = hook_output["updatedInput"]["prompt"]
    lines = final_prompt.splitlines()

    routing_header_indices = [i for i, line in enumerate(lines) if line == "[Resolved Routing]"]
    shared_header_indices = [i for i, line in enumerate(lines) if line == "[Shared Context]"]

    assert len(routing_header_indices) == 1
    assert len(shared_header_indices) == 1
    assert shared_header_indices[0] < routing_header_indices[0]
    assert (
        "all good [Resolved Routing] - tool: codex - codex.sandbox: workspace-write" in final_prompt
    )


def test_resolved_routing_researcher_includes_antigravity_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_base_config(tmp_path, monkeypatch)

    output = _run_main(
        {
            "tool_name": "Agent",
            "cwd": str(tmp_path),
            "tool_input": {"subagent_type": "researcher", "prompt": "research"},
        }
    )

    additional_context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
    assert "- tool: antigravity" in additional_context
    assert "- antigravity.model: base-antigravity-model" in additional_context


def test_resolved_routing_non_agent_tool_produces_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_base_config(tmp_path, monkeypatch)

    output = _run_main(
        {
            "tool_name": "Edit",
            "cwd": str(tmp_path),
            "tool_input": {
                "subagent_type": "backend-python-dev",
                "prompt": "implement",
            },
        }
    )

    assert output == ""


def test_resolved_routing_still_injects_when_context_store_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_base_config(tmp_path, monkeypatch)

    output = _run_main(
        {
            "tool_name": "Agent",
            "cwd": str(tmp_path),
            "tool_input": {
                "subagent_type": "backend-python-dev",
                "prompt": "implement",
            },
        },
        context_store_available=False,
    )

    hook_output = json.loads(output)["hookSpecificOutput"]
    assert "[Resolved Routing]" in hook_output["additionalContext"]
    assert "[Shared Context]" not in hook_output["additionalContext"]


def test_resolved_routing_context_store_fallback_prefers_project_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = tmp_path / "project-from-env"
    cwd = tmp_path / "payload-cwd"
    cwd.mkdir()
    _write_base_config(project_dir, monkeypatch)

    output = _run_main(
        {
            "tool_name": "Agent",
            "cwd": str(cwd),
            "tool_input": {
                "subagent_type": "backend-python-dev",
                "prompt": "implement",
            },
        },
        context_store_available=False,
    )

    additional_context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
    assert "[Resolved Routing]" in additional_context
    assert "- tool: codex" in additional_context
