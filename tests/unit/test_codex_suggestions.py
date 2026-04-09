"""codex-suggestions hooks のユニットテスト。"""

from __future__ import annotations

import io
import json

import pytest

from tests.module_loader import load_module

codex_write = load_module(
    "check_codex_write", "packages/codex-suggestions/hooks/check-codex-before-write.py"
)
codex_plan = load_module(
    "check_codex_plan", "packages/codex-suggestions/hooks/check-codex-after-plan.py"
)


# ======================================================================
# check-codex-before-write.py
# ======================================================================


class TestValidateInput:
    """validate_input のテスト。"""

    def test_valid(self):
        """正常な入力。"""
        assert codex_write.validate_input("src/main.py", "content") is True

    def test_empty_path(self):
        """空パスは False。"""
        assert codex_write.validate_input("", "content") is False

    def test_path_too_long(self):
        """パスが長すぎる場合は False。"""
        assert codex_write.validate_input("x" * 5000, "content") is False

    def test_content_too_long(self):
        """コンテンツが長すぎる場合は False。"""
        assert codex_write.validate_input("file.py", "x" * 1_100_000) is False

    def test_path_traversal(self):
        """パストラバーサルは False。"""
        assert codex_write.validate_input("../etc/passwd", "content") is False
        assert codex_write.validate_input("src/../../secret", "content") is False


class TestShouldSuggestCodex:
    """should_suggest_codex のテスト。"""

    def test_simple_edit_skipped(self):
        """SIMPLE_EDIT_PATTERNS のファイルはスキップ。"""
        assert codex_write.should_suggest_codex("README.md")[0] is False
        assert codex_write.should_suggest_codex("CHANGELOG.md")[0] is False
        assert codex_write.should_suggest_codex(".gitignore")[0] is False
        assert codex_write.should_suggest_codex("requirements.txt")[0] is False

    def test_design_file_path(self):
        """DESIGN_INDICATORS をパスに含むファイルは True。"""
        result, reason = codex_write.should_suggest_codex("DESIGN.md")
        assert result is True
        assert "DESIGN.md" in reason

    def test_core_path(self):
        """core/ パスは True。"""
        result, reason = codex_write.should_suggest_codex("core/engine.py")
        assert result is True

    def test_config_path(self):
        """config を含むパスは True。"""
        result, reason = codex_write.should_suggest_codex("settings/config.py")
        assert result is True

    def test_architecture_in_path(self):
        """architecture を含むパスは True。"""
        result, reason = codex_write.should_suggest_codex("docs/architecture/overview.md")
        assert result is True

    def test_large_content(self):
        """500文字以上の新規コンテンツは True。"""
        result, reason = codex_write.should_suggest_codex("normal.py", "x" * 600)
        assert result is True
        assert "significant content" in reason

    def test_content_with_design_indicator(self):
        """コンテンツに DESIGN_INDICATORS を含む場合は True。"""
        result, reason = codex_write.should_suggest_codex("file.py", "class MyClass:\n    pass")
        assert result is True

    def test_src_file_with_content(self):
        """src/ 内の 200 文字超のファイルは True。"""
        result, reason = codex_write.should_suggest_codex("src/new_module.py", "x" * 250)
        assert result is True

    def test_src_file_short_content(self):
        """src/ 内でも短いコンテンツは False。"""
        result, _ = codex_write.should_suggest_codex("src/tiny.py", "pass")
        assert result is False

    def test_plain_file_no_content(self):
        """通常のパスでコンテンツなしは False。"""
        result, _ = codex_write.should_suggest_codex("utils/helper.py")
        assert result is False

    def test_plain_file_short_content(self):
        """通常のパスで短いコンテンツは False。"""
        result, _ = codex_write.should_suggest_codex("utils/helper.py", "x = 1")
        assert result is False


class TestBuildCodexCommand:
    """_build_codex_command のテスト。"""

    def test_defaults(self):
        """デフォルト値でコマンドを構築。"""
        result = codex_write._build_codex_command({})
        assert "gpt-5.3-codex" in result
        assert "read-only" in result
        assert "--full-auto" in result

    def test_custom_config(self):
        """カスタム設定でコマンドを構築。"""
        config = {
            "codex": {
                "model": "o3-pro",
                "sandbox": {"analysis": "workspace-write"},
                "flags": "--quiet",
            }
        }
        result = codex_write._build_codex_command(config)
        assert "o3-pro" in result
        assert "workspace-write" in result
        assert "--quiet" in result


class TestCodexWriteMain:
    """check-codex-before-write main() のテスト。"""

    def test_codex_disabled_exits(self, monkeypatch):
        """Codex 無効時は提案なしで exit(0)。"""
        monkeypatch.setattr(codex_write, "is_cli_enabled", lambda tool, config: False)
        monkeypatch.setattr(codex_write, "load_package_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_input": {"file_path": "core/main.py", "content": "class Foo: pass"},
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            codex_write.main()

    def test_invalid_input_exits(self, monkeypatch):
        """不正な入力は exit(0)。"""
        monkeypatch.setattr(codex_write, "is_cli_enabled", lambda *a: True)
        monkeypatch.setattr(codex_write, "load_package_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_input": {"file_path": "../etc/passwd", "content": "hack"},
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            codex_write.main()

    def test_suggestion_output(self, monkeypatch, capsys):
        """提案がある場合、hookSpecificOutput を出力。"""
        monkeypatch.setattr(codex_write, "is_cli_enabled", lambda *a: True)
        monkeypatch.setattr(codex_write, "load_package_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_input": {
                            "file_path": "core/engine.py",
                            "content": "class Engine: pass",
                        },
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            codex_write.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "Codex Suggestion" in output["hookSpecificOutput"]["additionalContext"]


# ======================================================================
# check-codex-after-plan.py
# ======================================================================


class TestIsPlanAgentTask:
    """is_plan_agent_task のテスト。"""

    def test_planner_subagent(self):
        """subagent_type=planner は True。"""
        assert codex_plan.is_plan_agent_task({"subagent_type": "planner"}) is True

    def test_plan_subagent(self):
        """subagent_type=plan は True。"""
        assert codex_plan.is_plan_agent_task({"subagent_type": "plan"}) is True

    def test_case_insensitive(self):
        """大文字小文字を区別しない。"""
        assert codex_plan.is_plan_agent_task({"subagent_type": "Planner"}) is True

    def test_non_plan_agent(self):
        """plan 以外は prompt キーワードで判定。"""
        assert codex_plan.is_plan_agent_task({"subagent_type": "researcher"}) is False

    def test_plan_keyword_in_prompt_japanese(self):
        """日本語の計画キーワードで True。"""
        assert (
            codex_plan.is_plan_agent_task({"subagent_type": "other", "prompt": "実装計画を立てて"})
            is True
        )
        assert (
            codex_plan.is_plan_agent_task({"subagent_type": "other", "prompt": "プランを作成"})
            is True
        )

    def test_plan_keyword_in_prompt_english(self):
        """英語の plan キーワードで True。"""
        assert (
            codex_plan.is_plan_agent_task(
                {"subagent_type": "other", "prompt": "create implementation plan"}
            )
            is True
        )

    def test_no_plan_keywords(self):
        """キーワードなしは False。"""
        assert (
            codex_plan.is_plan_agent_task({"subagent_type": "other", "prompt": "implement feature"})
            is False
        )


class TestCodexPlanMain:
    """check-codex-after-plan main() のテスト。"""

    def test_non_agent_tool_exits(self, monkeypatch):
        """Agent/Task 以外は exit(0)。"""
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash"})))
        with pytest.raises(SystemExit, match="0"):
            codex_plan.main()

    def test_codex_disabled_exits(self, monkeypatch):
        """Codex 無効時は exit(0)。"""
        monkeypatch.setattr(codex_plan, "is_cli_enabled", lambda *a: False)
        monkeypatch.setattr(codex_plan, "load_package_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "Agent",
                        "tool_input": {"subagent_type": "planner"},
                        "tool_response": "plan done",
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            codex_plan.main()

    def test_error_response_exits(self, monkeypatch):
        """エラーレスポンスは exit(0)。"""
        monkeypatch.setattr(codex_plan, "is_cli_enabled", lambda *a: True)
        monkeypatch.setattr(codex_plan, "load_package_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "Agent",
                        "tool_input": {"subagent_type": "planner"},
                        "tool_response": "An error occurred and the task failed",
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            codex_plan.main()

    def test_successful_plan_outputs_suggestion(self, monkeypatch, capsys):
        """正常なプラン完了後に提案を出力。"""
        monkeypatch.setattr(codex_plan, "is_cli_enabled", lambda *a: True)
        monkeypatch.setattr(codex_plan, "load_package_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "Agent",
                        "tool_input": {"subagent_type": "planner"},
                        "tool_response": "Plan created with 5 phases",
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            codex_plan.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "Codex Review Suggestion" in output["hookSpecificOutput"]["additionalContext"]
