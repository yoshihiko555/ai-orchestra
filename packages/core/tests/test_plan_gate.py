"""plan gate hooks のテスト。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.module_loader import REPO_ROOT, load_module

HOOKS_DIR = REPO_ROOT / "packages" / "core" / "hooks"

# hook スクリプト内の `from hook_common import ...` を解決できるようにする
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

set_plan_gate = load_module("set_plan_gate", "packages/core/hooks/set-plan-gate.py")
check_plan_gate = load_module("check_plan_gate", "packages/core/hooks/check-plan-gate.py")
clear_plan_gate = load_module("clear_plan_gate", "packages/core/hooks/clear-plan-gate.py")


def _state_dir(project_dir: Path) -> Path:
    return project_dir / ".claude" / "state"


def _gate_path(project_dir: Path) -> Path:
    return _state_dir(project_dir) / "plan-gate.json"


def _write_gate_file(project_dir: Path, *, pending: bool = True, agent: str = "planner") -> Path:
    path = _gate_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pending": pending, "agent": agent, "set_at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    return path


def _run_hook(
    script_name: str, payload: dict[str, Any], project_dir: Path
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AI_ORCHESTRA_DIR"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / script_name)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        cwd=str(project_dir),
        check=False,
    )


class TestSetPlanGate:
    def test_plan_agents_contains_exact_expected_set(self) -> None:
        assert set_plan_gate.PLAN_AGENTS == {"plan", "planner"}

    def test_get_state_dir_returns_expected_path_with_cwd(self, tmp_path: Path) -> None:
        result = set_plan_gate._get_state_dir({"cwd": str(tmp_path)})
        assert result == str(_state_dir(tmp_path))

    def test_get_state_dir_returns_empty_when_cwd_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        assert set_plan_gate._get_state_dir({}) == ""

    def test_subprocess_creates_gate_for_planner(self, tmp_path: Path) -> None:
        payload = {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "planner"},
            "tool_response": "## Plan: Feature X\n### Steps\n1. Step A\n2. Step B",
            "cwd": str(tmp_path),
        }
        result = _run_hook("set-plan-gate.py", payload, tmp_path)
        gate_path = _gate_path(tmp_path)

        assert result.returncode == 0
        assert gate_path.is_file()

        gate_data = json.loads(gate_path.read_text(encoding="utf-8"))
        assert gate_data["pending"] is True
        assert gate_data["agent"] == "planner"
        assert isinstance(gate_data.get("set_at"), str)
        assert gate_data["set_at"]

        output = json.loads(result.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        assert "[Plan Gate]" in context

    def test_subprocess_does_not_create_gate_when_response_empty(self, tmp_path: Path) -> None:
        payload = {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "planner"},
            "tool_response": "",
            "cwd": str(tmp_path),
        }
        result = _run_hook("set-plan-gate.py", payload, tmp_path)

        assert result.returncode == 0
        assert not _gate_path(tmp_path).exists()

    def test_subprocess_does_not_create_gate_when_response_has_nonzero_exit_code(
        self, tmp_path: Path
    ) -> None:
        payload = {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "planner"},
            "tool_response": {"exit_code": 1, "output": "Agent crashed"},
            "cwd": str(tmp_path),
        }
        result = _run_hook("set-plan-gate.py", payload, tmp_path)

        assert result.returncode == 0
        assert not _gate_path(tmp_path).exists()

    def test_subprocess_does_not_create_gate_when_response_has_error_key(
        self, tmp_path: Path
    ) -> None:
        payload = {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "planner"},
            "tool_response": {"error": "Agent failed to execute"},
            "cwd": str(tmp_path),
        }
        result = _run_hook("set-plan-gate.py", payload, tmp_path)

        assert result.returncode == 0
        assert not _gate_path(tmp_path).exists()

    def test_subprocess_creates_gate_when_response_is_empty_dict(self, tmp_path: Path) -> None:
        """空 dict は有効なレスポンスとみなし、ゲートを設定する。"""
        payload = {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "planner"},
            "tool_response": {},
            "cwd": str(tmp_path),
        }
        result = _run_hook("set-plan-gate.py", payload, tmp_path)

        assert result.returncode == 0
        assert _gate_path(tmp_path).is_file()

    def test_subprocess_creates_gate_when_error_is_null(self, tmp_path: Path) -> None:
        """error=null, exit_code=0 は成功扱いで、ゲートを設定する。"""
        payload = {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "planner"},
            "tool_response": {"error": None, "exit_code": 0},
            "cwd": str(tmp_path),
        }
        result = _run_hook("set-plan-gate.py", payload, tmp_path)

        assert result.returncode == 0
        assert _gate_path(tmp_path).is_file()

    def test_subprocess_creates_gate_when_response_contains_error_word_in_text(
        self, tmp_path: Path
    ) -> None:
        """'error handling' のような文字列を含む正常レスポンスではゲートが設定されること。"""
        payload = {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "planner"},
            "tool_response": "Plan includes error handling strategy and failed-login recovery",
            "cwd": str(tmp_path),
        }
        result = _run_hook("set-plan-gate.py", payload, tmp_path)

        assert result.returncode == 0
        assert _gate_path(tmp_path).is_file()

    def test_subprocess_does_not_create_gate_for_non_plan_agent(self, tmp_path: Path) -> None:
        payload = {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "frontend-dev"},
            "cwd": str(tmp_path),
        }
        result = _run_hook("set-plan-gate.py", payload, tmp_path)

        assert result.returncode == 0
        assert not _gate_path(tmp_path).exists()

    def test_subprocess_does_not_create_gate_for_non_task_tool(self, tmp_path: Path) -> None:
        payload = {
            "tool_name": "Edit",
            "tool_input": {"subagent_type": "planner"},
            "cwd": str(tmp_path),
        }
        result = _run_hook("set-plan-gate.py", payload, tmp_path)

        assert result.returncode == 0
        assert not _gate_path(tmp_path).exists()


class TestCheckPlanGate:
    def test_implementation_agents_contains_expected_set(self) -> None:
        assert check_plan_gate.IMPLEMENTATION_AGENTS == {
            "frontend-dev",
            "backend-python-dev",
            "backend-go-dev",
            "ai-dev",
            "rag-engineer",
            "debugger",
            "tester",
            "spec-writer",
        }

    def test_warn_agents_contains_general_purpose_only(self) -> None:
        assert check_plan_gate.WARN_AGENTS == {"general-purpose"}

    def test_get_gate_path_returns_expected_path(self, tmp_path: Path) -> None:
        result = check_plan_gate._get_gate_path({"cwd": str(tmp_path)})
        assert result == str(_gate_path(tmp_path))

    def test_subprocess_exits_2_for_pending_gate_and_implementation_agent(
        self, tmp_path: Path
    ) -> None:
        _write_gate_file(tmp_path, pending=True, agent="planner")
        payload = {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "frontend-dev"},
            "cwd": str(tmp_path),
        }
        result = _run_hook("check-plan-gate.py", payload, tmp_path)

        assert result.returncode == 2
        assert "[Plan Gate]" in result.stderr
        assert "frontend-dev" in result.stderr

    def test_subprocess_warns_for_pending_gate_and_general_purpose(self, tmp_path: Path) -> None:
        _write_gate_file(tmp_path, pending=True, agent="planner")
        payload = {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "general-purpose"},
            "cwd": str(tmp_path),
        }
        result = _run_hook("check-plan-gate.py", payload, tmp_path)

        assert result.returncode == 0
        output = json.loads(result.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        assert "[Plan Gate Warning]" in context
        assert "general-purpose" in context

    def test_subprocess_allows_implementation_agent_when_no_gate_file(self, tmp_path: Path) -> None:
        payload = {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "frontend-dev"},
            "cwd": str(tmp_path),
        }
        result = _run_hook("check-plan-gate.py", payload, tmp_path)

        assert result.returncode == 0
        assert not result.stdout.strip()
        assert not result.stderr.strip()

    def test_subprocess_allows_non_implementation_agent_when_gate_pending(
        self, tmp_path: Path
    ) -> None:
        _write_gate_file(tmp_path, pending=True, agent="planner")
        payload = {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "planner"},
            "cwd": str(tmp_path),
        }
        result = _run_hook("check-plan-gate.py", payload, tmp_path)

        assert result.returncode == 0
        assert not result.stdout.strip()
        assert not result.stderr.strip()


class TestClearPlanGate:
    def test_get_gate_path_returns_expected_path(self, tmp_path: Path) -> None:
        result = clear_plan_gate._get_gate_path({"cwd": str(tmp_path)})
        assert result == str(_gate_path(tmp_path))

    def test_subprocess_removes_gate_file_when_exists(self, tmp_path: Path) -> None:
        gate_path = _write_gate_file(tmp_path, pending=True, agent="planner")
        payload = {"cwd": str(tmp_path)}
        result = _run_hook("clear-plan-gate.py", payload, tmp_path)

        assert result.returncode == 0
        assert not gate_path.exists()

    def test_subprocess_no_error_when_gate_file_does_not_exist(self, tmp_path: Path) -> None:
        payload = {"cwd": str(tmp_path)}
        result = _run_hook("clear-plan-gate.py", payload, tmp_path)

        assert result.returncode == 0
        assert not _gate_path(tmp_path).exists()
