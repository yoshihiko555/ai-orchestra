"""plan gate hooks（check/set/clear）のユニットテスト。"""

from __future__ import annotations

import io
import json
import os
import sys

import pytest

from tests.module_loader import load_module

check_gate = load_module("check_plan_gate", "packages/core/hooks/check-plan-gate.py")
set_gate = load_module("set_plan_gate", "packages/core/hooks/set-plan-gate.py")
clear_gate = load_module("clear_plan_gate", "packages/core/hooks/clear-plan-gate.py")


def _make_stdin(data: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """stdin を JSON データでモックする。"""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))


# ======================================================================
# check-plan-gate.py
# ======================================================================


class TestCheckPlanGateGetPath:
    """_get_gate_path のテスト。"""

    def test_cwd_provided(self):
        """cwd がある場合、そこから gate path を組み立てる。"""
        result = check_gate._get_gate_path({"cwd": "/project"})
        assert result == "/project/.claude/state/plan-gate.json"

    def test_env_fallback(self, monkeypatch):
        """cwd がなく CLAUDE_PROJECT_DIR がある場合、環境変数を使う。"""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/env-project")
        result = check_gate._get_gate_path({})
        assert result == "/env-project/.claude/state/plan-gate.json"

    def test_no_cwd_no_env(self, monkeypatch):
        """cwd も環境変数もない場合、空文字を返す。"""
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        result = check_gate._get_gate_path({"cwd": ""})
        assert result == ""


class TestCheckPlanGateMain:
    """check-plan-gate main() のテスト。"""

    def test_non_agent_tool_exits_0(self, monkeypatch):
        """Agent/Task 以外のツールは exit(0) する。"""
        _make_stdin({"tool_name": "Edit", "tool_input": {}}, monkeypatch)
        with pytest.raises(SystemExit, match="0"):
            check_gate.main()

    def test_non_implementation_agent_exits_0(self, monkeypatch, tmp_path):
        """実装系でもWARNでもないエージェントは exit(0)。"""
        _make_stdin(
            {"tool_name": "Agent", "tool_input": {"subagent_type": "researcher"}, "cwd": str(tmp_path)},
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="0"):
            check_gate.main()

    def test_no_gate_path_exits_0(self, monkeypatch):
        """gate path が空の場合、exit(0)。"""
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        _make_stdin(
            {"tool_name": "Agent", "tool_input": {"subagent_type": "backend-python-dev"}, "cwd": ""},
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="0"):
            check_gate.main()

    def test_no_gate_file_exits_0(self, monkeypatch, tmp_path):
        """gate ファイルがない場合（pending=False と同等）、exit(0)。"""
        _make_stdin(
            {"tool_name": "Agent", "tool_input": {"subagent_type": "backend-python-dev"}, "cwd": str(tmp_path)},
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="0"):
            check_gate.main()

    def test_gate_not_pending_exits_0(self, monkeypatch, tmp_path):
        """gate が pending=False の場合、exit(0)。"""
        state_dir = tmp_path / ".claude" / "state"
        state_dir.mkdir(parents=True)
        gate_path = state_dir / "plan-gate.json"
        gate_path.write_text(json.dumps({"pending": False}), encoding="utf-8")

        _make_stdin(
            {"tool_name": "Agent", "tool_input": {"subagent_type": "backend-python-dev"}, "cwd": str(tmp_path)},
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="0"):
            check_gate.main()

    def test_implementation_agent_blocked(self, monkeypatch, tmp_path):
        """pending=True の gate がある場合、実装系エージェントは exit(2) でブロックされる。"""
        state_dir = tmp_path / ".claude" / "state"
        state_dir.mkdir(parents=True)
        gate_path = state_dir / "plan-gate.json"
        gate_path.write_text(json.dumps({"pending": True, "agent": "planner"}), encoding="utf-8")

        _make_stdin(
            {"tool_name": "Agent", "tool_input": {"subagent_type": "backend-python-dev"}, "cwd": str(tmp_path)},
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="2"):
            check_gate.main()

    def test_warn_agent_outputs_warning(self, monkeypatch, tmp_path, capsys):
        """general-purpose は警告出力して exit(0)。"""
        state_dir = tmp_path / ".claude" / "state"
        state_dir.mkdir(parents=True)
        gate_path = state_dir / "plan-gate.json"
        gate_path.write_text(json.dumps({"pending": True, "agent": "planner"}), encoding="utf-8")

        _make_stdin(
            {"tool_name": "Agent", "tool_input": {"subagent_type": "general-purpose"}, "cwd": str(tmp_path)},
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="0"):
            check_gate.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "Plan Gate Warning" in output["hookSpecificOutput"]["additionalContext"]

    def test_task_tool_name_also_works(self, monkeypatch, tmp_path):
        """後方互換: tool_name=Task でも動作する。"""
        state_dir = tmp_path / ".claude" / "state"
        state_dir.mkdir(parents=True)
        gate_path = state_dir / "plan-gate.json"
        gate_path.write_text(json.dumps({"pending": True}), encoding="utf-8")

        _make_stdin(
            {"tool_name": "Task", "tool_input": {"subagent_type": "frontend-dev"}, "cwd": str(tmp_path)},
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="2"):
            check_gate.main()


# ======================================================================
# set-plan-gate.py
# ======================================================================


class TestSetPlanGateMain:
    """set-plan-gate main() のテスト。"""

    def test_non_agent_tool_exits_0(self, monkeypatch):
        """Agent/Task 以外は exit(0)。"""
        _make_stdin({"tool_name": "Bash", "tool_input": {}}, monkeypatch)
        with pytest.raises(SystemExit, match="0"):
            set_gate.main()

    def test_non_plan_agent_exits_0(self, monkeypatch):
        """plan エージェント以外は exit(0)。"""
        _make_stdin(
            {"tool_name": "Agent", "tool_input": {"subagent_type": "researcher"}, "tool_response": "ok"},
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="0"):
            set_gate.main()

    def test_empty_response_exits_0(self, monkeypatch):
        """tool_response が空の場合、gate を設定しない。"""
        _make_stdin(
            {"tool_name": "Agent", "tool_input": {"subagent_type": "planner"}, "tool_response": ""},
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="0"):
            set_gate.main()

    def test_none_response_exits_0(self, monkeypatch):
        """tool_response が None の場合、gate を設定しない。"""
        _make_stdin(
            {"tool_name": "Agent", "tool_input": {"subagent_type": "planner"}, "tool_response": None},
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="0"):
            set_gate.main()

    def test_error_response_exits_0(self, monkeypatch):
        """tool_response にエラーがある場合、gate を設定しない。"""
        _make_stdin(
            {
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "planner"},
                "tool_response": {"error": "something went wrong"},
            },
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="0"):
            set_gate.main()

    def test_nonzero_exit_code_exits_0(self, monkeypatch):
        """exit_code が非 0 の場合、gate を設定しない。"""
        _make_stdin(
            {
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "planner"},
                "tool_response": {"exit_code": 1},
            },
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="0"):
            set_gate.main()

    def test_successful_plan_sets_gate(self, monkeypatch, tmp_path, capsys):
        """正常な plan 完了後に gate ファイルを作成する。"""
        _make_stdin(
            {
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "planner"},
                "tool_response": "Plan created successfully",
                "cwd": str(tmp_path),
            },
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="0"):
            set_gate.main()

        gate_path = tmp_path / ".claude" / "state" / "plan-gate.json"
        assert gate_path.exists()
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        assert gate["pending"] is True
        assert gate["agent"] == "planner"
        assert "set_at" in gate

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "Plan Gate" in output["hookSpecificOutput"]["additionalContext"]

    def test_no_cwd_exits_0(self, monkeypatch):
        """cwd がない場合、gate を設定しない。"""
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        _make_stdin(
            {
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "plan"},
                "tool_response": "ok",
                "cwd": "",
            },
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="0"):
            set_gate.main()


# ======================================================================
# clear-plan-gate.py
# ======================================================================


class TestClearPlanGateMain:
    """clear-plan-gate main() のテスト。"""

    def test_no_gate_file_exits_0(self, monkeypatch, tmp_path):
        """gate ファイルが存在しない場合、exit(0)。"""
        _make_stdin({"cwd": str(tmp_path)}, monkeypatch)
        with pytest.raises(SystemExit, match="0"):
            clear_gate.main()

    def test_removes_gate_file(self, monkeypatch, tmp_path):
        """gate ファイルを削除する。"""
        state_dir = tmp_path / ".claude" / "state"
        state_dir.mkdir(parents=True)
        gate_path = state_dir / "plan-gate.json"
        gate_path.write_text(json.dumps({"pending": True}), encoding="utf-8")

        _make_stdin({"cwd": str(tmp_path)}, monkeypatch)
        with pytest.raises(SystemExit, match="0"):
            clear_gate.main()

        assert not gate_path.exists()

    def test_no_cwd_exits_0(self, monkeypatch):
        """cwd がない場合、exit(0)。"""
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        _make_stdin({"cwd": ""}, monkeypatch)
        with pytest.raises(SystemExit, match="0"):
            clear_gate.main()


# ======================================================================
# 状態遷移の統合テスト
# ======================================================================


class TestPlanGateFlow:
    """plan gate の状態遷移フローテスト。"""

    def test_set_check_clear_flow(self, monkeypatch, tmp_path):
        """set → check blocks → clear → check passes の全フロー。"""
        # 1. Plan 完了: gate を設定
        _make_stdin(
            {
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "planner"},
                "tool_response": "Plan done",
                "cwd": str(tmp_path),
            },
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="0"):
            set_gate.main()

        gate_path = tmp_path / ".claude" / "state" / "plan-gate.json"
        assert gate_path.exists()

        # 2. 実装エージェント呼び出し: ブロックされる
        _make_stdin(
            {
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "backend-python-dev"},
                "cwd": str(tmp_path),
            },
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="2"):
            check_gate.main()

        # 3. ユーザーがメッセージ送信: gate を解除
        _make_stdin({"cwd": str(tmp_path)}, monkeypatch)
        with pytest.raises(SystemExit, match="0"):
            clear_gate.main()

        assert not gate_path.exists()

        # 4. 実装エージェント呼び出し: 今度は通る
        _make_stdin(
            {
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "backend-python-dev"},
                "cwd": str(tmp_path),
            },
            monkeypatch,
        )
        with pytest.raises(SystemExit, match="0"):
            check_gate.main()
