"""各 context hook のテスト。

テスト対象:
  - capture-task-result.py
  - inject-shared-context.py
  - update-working-context.py
  - cleanup-session-context.py
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tests.module_loader import load_module

# ---------------------------------------------------------------------------
# モジュールロード
# ---------------------------------------------------------------------------

capture_mod = load_module("capture_task_result", "packages/core/hooks/capture-task-result.py")
inject_mod = load_module("inject_shared_context", "packages/core/hooks/inject-shared-context.py")
update_mod = load_module("update_working_context", "packages/core/hooks/update-working-context.py")
cleanup_mod = load_module(
    "cleanup_session_context", "packages/core/hooks/cleanup-session-context.py"
)

context_store = load_module("context_store", "packages/core/hooks/context_store.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entries_dir(project_dir: Path) -> Path:
    return project_dir / ".claude" / "context" / "session" / "entries"


def _working_context_path(project_dir: Path) -> Path:
    return project_dir / ".claude" / "context" / "shared" / "working-context.json"


def _session_dir(project_dir: Path) -> Path:
    return project_dir / ".claude" / "context" / "session"


# ---------------------------------------------------------------------------
# capture-task-result.py
# ---------------------------------------------------------------------------


class TestExtractAgentId:
    def test_returns_subagent_type_when_present(self) -> None:
        # Arrange
        tool_input = {"subagent_type": "tester", "prompt": "run tests"}

        # Act
        result = capture_mod.extract_agent_id(tool_input)

        # Assert
        assert result == "tester"

    def test_returns_unknown_when_subagent_type_missing(self) -> None:
        # Arrange
        tool_input = {"prompt": "run tests"}

        # Act
        result = capture_mod.extract_agent_id(tool_input)

        # Assert
        assert result == "unknown"

    def test_returns_unknown_when_subagent_type_empty(self) -> None:
        # Arrange
        tool_input = {"subagent_type": "", "prompt": "run tests"}

        # Act
        result = capture_mod.extract_agent_id(tool_input)

        # Assert
        assert result == "unknown"


class TestExtractTaskName:
    def test_returns_description_when_present(self) -> None:
        # Arrange
        tool_input = {"description": "Run integration tests", "prompt": "some prompt"}

        # Act
        result = capture_mod.extract_task_name(tool_input)

        # Assert
        assert result == "Run integration tests"

    def test_falls_back_to_prompt_prefix_when_no_description(self) -> None:
        # Arrange
        tool_input = {"prompt": "A" * 100}

        # Act
        result = capture_mod.extract_task_name(tool_input)

        # Assert
        assert result == "A" * 50

    def test_returns_empty_when_both_missing(self) -> None:
        # Arrange
        tool_input = {}

        # Act
        result = capture_mod.extract_task_name(tool_input)

        # Assert
        assert result == ""


class TestTruncateSummary:
    def test_returns_text_as_is_when_within_limit(self) -> None:
        # Arrange
        text = "short summary"

        # Act
        result = capture_mod.truncate_summary(text)

        # Assert
        assert result == text

    def test_truncates_text_exceeding_2000_chars(self) -> None:
        # Arrange
        text = "x" * 3000

        # Act
        result = capture_mod.truncate_summary(text)

        # Assert
        assert len(result) == 2000

    def test_handles_exactly_2000_chars(self) -> None:
        # Arrange
        text = "y" * 2000

        # Act
        result = capture_mod.truncate_summary(text)

        # Assert
        assert len(result) == 2000

    def test_converts_non_string_to_string(self) -> None:
        # Act
        result = capture_mod.truncate_summary(12345)

        # Assert
        assert isinstance(result, str)
        assert result == "12345"


class TestCaptureTaskResultMain:
    def test_writes_entry_for_task_tool(self, tmp_path: Path) -> None:
        # Arrange
        stdin_data = json.dumps(
            {
                "tool_name": "Task",
                "cwd": str(tmp_path),
                "tool_input": {
                    "subagent_type": "tester",
                    "description": "Run tests",
                    "prompt": "pytest -q",
                },
                "tool_response": "All tests passed.",
            }
        )

        # Act
        with patch.object(sys, "stdin", StringIO(stdin_data)):
            with patch.object(capture_mod, "_CONTEXT_STORE_AVAILABLE", True):
                capture_mod.main()

        # Assert – ファイル名は {agent_id}_{timestamp}.json 形式
        entries = list(_entries_dir(tmp_path).glob("tester_*.json"))
        assert len(entries) == 1
        stored = json.loads(entries[0].read_text(encoding="utf-8"))
        assert stored["agent_id"] == "tester"
        assert stored["task_name"] == "Run tests"
        assert stored["summary"] == "All tests passed."
        assert stored["status"] == "done"

    def test_does_nothing_for_non_task_tool(self, tmp_path: Path) -> None:
        # Arrange
        stdin_data = json.dumps(
            {
                "tool_name": "Edit",
                "cwd": str(tmp_path),
                "tool_input": {"file_path": "src/foo.py"},
                "tool_response": "done",
            }
        )

        # Act
        with patch.object(sys, "stdin", StringIO(stdin_data)):
            with patch.object(capture_mod, "_CONTEXT_STORE_AVAILABLE", True):
                capture_mod.main()

        # Assert – no entries written
        assert not _entries_dir(tmp_path).exists()


# ---------------------------------------------------------------------------
# inject-shared-context.py
# ---------------------------------------------------------------------------


class TestBuildEntriesSection:
    def test_returns_empty_string_for_empty_entries(self) -> None:
        # Act
        result = inject_mod.build_entries_section([])

        # Assert
        assert result == ""

    def test_builds_section_with_single_entry(self) -> None:
        # Arrange
        entries = [
            {
                "agent_id": "tester",
                "task_name": "Run tests",
                "summary": "All passed.",
                "timestamp": "2026-01-01T00:00:00+00:00",
            }
        ]

        # Act
        result = inject_mod.build_entries_section(entries)

        # Assert
        assert "## Previous Agent Results" in result
        assert "tester" in result
        assert "Run tests" in result

    def test_limits_to_max_five_entries(self) -> None:
        # Arrange
        entries = [
            {
                "agent_id": f"agent-{i}",
                "task_name": f"task-{i}",
                "summary": "done",
                "timestamp": f"2026-01-0{i + 1}T00:00:00+00:00",
            }
            for i in range(7)
        ]

        # Act
        result = inject_mod.build_entries_section(entries)

        # Assert – 5 lines after the header
        lines = result.split("\n")
        entry_lines = [l for l in lines if l.startswith("- ")]
        assert len(entry_lines) == 5
        assert "agent-0" not in result
        assert "agent-1" not in result
        for i in range(2, 7):
            assert f"agent-{i}" in result

    def test_truncates_long_summary(self) -> None:
        # Arrange
        long_summary = "z" * 500
        entries = [
            {
                "agent_id": "agent-x",
                "task_name": "task-x",
                "summary": long_summary,
                "timestamp": "2026-01-01T00:00:00+00:00",
            }
        ]

        # Act
        result = inject_mod.build_entries_section(entries)

        # Assert – summary truncated to 200 chars + "..."
        assert "z" * 200 in result
        assert "..." in result


class TestBuildWorkingContextSection:
    def test_returns_empty_string_for_empty_context(self) -> None:
        # Act
        result = inject_mod.build_working_context_section({})

        # Assert
        assert result == ""

    def test_includes_modified_files(self) -> None:
        # Arrange
        ctx = {"modified_files": ["src/a.py", "src/b.py"]}

        # Act
        result = inject_mod.build_working_context_section(ctx)

        # Assert
        assert "src/a.py" in result
        assert "src/b.py" in result

    def test_includes_current_phase(self) -> None:
        # Arrange
        ctx = {"current_phase": "implementation"}

        # Act
        result = inject_mod.build_working_context_section(ctx)

        # Assert
        assert "implementation" in result

    def test_includes_recent_decisions(self) -> None:
        # Arrange
        ctx = {"recent_decisions": "Use PostgreSQL"}

        # Act
        result = inject_mod.build_working_context_section(ctx)

        # Assert
        assert "Use PostgreSQL" in result

    def test_returns_empty_when_all_fields_empty(self) -> None:
        # Arrange
        ctx = {"updated_at": "2026-01-01T00:00:00+00:00"}

        # Act
        result = inject_mod.build_working_context_section(ctx)

        # Assert
        assert result == ""


class TestBuildInjectionText:
    def test_returns_empty_when_both_empty(self) -> None:
        # Act
        result = inject_mod.build_injection_text([], {})

        # Assert
        assert result == ""

    def test_returns_text_when_only_entries(self) -> None:
        # Arrange
        entries = [
            {
                "agent_id": "tester",
                "task_name": "tests",
                "summary": "ok",
                "timestamp": "2026-01-01T00:00:00+00:00",
            }
        ]

        # Act
        result = inject_mod.build_injection_text(entries, {})

        # Assert
        assert "[Shared Context]" in result
        assert "Previous Agent Results" in result

    def test_returns_text_when_only_working_context(self) -> None:
        # Arrange
        ctx = {"current_phase": "review"}

        # Act
        result = inject_mod.build_injection_text([], ctx)

        # Assert
        assert "[Shared Context]" in result
        assert "Working Context" in result

    def test_combines_both_sections(self) -> None:
        # Arrange
        entries = [
            {
                "agent_id": "tester",
                "task_name": "tests",
                "summary": "ok",
                "timestamp": "2026-01-01T00:00:00+00:00",
            }
        ]
        ctx = {"current_phase": "review"}

        # Act
        result = inject_mod.build_injection_text(entries, ctx)

        # Assert
        assert "Previous Agent Results" in result
        assert "Working Context" in result


class TestInjectSharedContextMain:
    def test_appends_injection_to_prompt(self, tmp_path: Path) -> None:
        # Arrange
        context_store.write_entry(
            str(tmp_path),
            "tester",
            {
                "agent_id": "tester",
                "task_name": "run",
                "summary": "done",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "status": "done",
            },
        )
        stdin_data = json.dumps(
            {
                "tool_name": "Task",
                "cwd": str(tmp_path),
                "tool_input": {"subagent_type": "backend-python-dev", "prompt": "implement X"},
            }
        )

        # Act
        output_buf = StringIO()
        with patch.object(sys, "stdin", StringIO(stdin_data)):
            with patch.object(sys, "stdout", output_buf):
                with patch.object(inject_mod, "_CONTEXT_STORE_AVAILABLE", True):
                    inject_mod.main()

        # Assert
        output = output_buf.getvalue()
        assert output  # something was printed
        parsed = json.loads(output)
        hook_output = parsed["hookSpecificOutput"]
        assert "[Shared Context]" in hook_output["additionalContext"]
        assert "[Shared Context]" in hook_output["updatedInput"]["prompt"]

    def test_does_nothing_for_non_task_tool(self, tmp_path: Path) -> None:
        # Arrange
        context_store.write_entry(
            str(tmp_path),
            "tester",
            {
                "agent_id": "tester",
                "task_name": "run",
                "summary": "done",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "status": "done",
            },
        )
        stdin_data = json.dumps(
            {
                "tool_name": "Edit",
                "cwd": str(tmp_path),
                "tool_input": {"file_path": "src/foo.py"},
            }
        )

        # Act
        output_buf = StringIO()
        with patch.object(sys, "stdin", StringIO(stdin_data)):
            with patch.object(sys, "stdout", output_buf):
                with patch.object(inject_mod, "_CONTEXT_STORE_AVAILABLE", True):
                    inject_mod.main()

        # Assert – no output
        assert output_buf.getvalue() == ""


# ---------------------------------------------------------------------------
# update-working-context.py
# ---------------------------------------------------------------------------


class TestToRelativePath:
    def test_removes_project_dir_prefix(self) -> None:
        # Act
        result = update_mod.to_relative_path("/project/src/foo.py", "/project")

        # Assert
        assert result == "src/foo.py"

    def test_returns_path_unchanged_when_no_prefix(self) -> None:
        # Act
        result = update_mod.to_relative_path("/other/src/foo.py", "/project")

        # Assert
        assert result == "/other/src/foo.py"

    def test_returns_path_unchanged_when_only_prefix_matches(self) -> None:
        # Act
        result = update_mod.to_relative_path("/project-other/src/foo.py", "/project")

        # Assert
        assert result == "/project-other/src/foo.py"

    def test_returns_path_unchanged_when_project_dir_empty(self) -> None:
        # Act
        result = update_mod.to_relative_path("/project/src/foo.py", "")

        # Assert
        assert result == "/project/src/foo.py"


class TestIsClaudeInternal:
    def test_returns_true_for_claude_subpath(self) -> None:
        assert update_mod.is_claude_internal(".claude/Plans.md") is True

    def test_returns_true_for_dot_claude_only(self) -> None:
        assert update_mod.is_claude_internal(".claude") is True

    def test_returns_false_for_non_claude_path(self) -> None:
        assert update_mod.is_claude_internal("src/main.py") is False

    def test_returns_false_for_similar_prefix(self) -> None:
        assert update_mod.is_claude_internal(".claude_backup/foo.py") is False

    def test_handles_windows_separators(self) -> None:
        assert update_mod.is_claude_internal(".claude\\Plans.md") is True


class TestUpdateWorkingContextMain:
    def test_updates_modified_files_on_edit(self, tmp_path: Path) -> None:
        # Arrange
        stdin_data = json.dumps(
            {
                "tool_name": "Edit",
                "cwd": str(tmp_path),
                "tool_input": {"file_path": str(tmp_path / "src" / "foo.py")},
            }
        )

        # Act
        with patch.object(sys, "stdin", StringIO(stdin_data)):
            with patch.object(update_mod, "_CONTEXT_STORE_AVAILABLE", True):
                with patch.object(
                    update_mod,
                    "read_hook_input",
                    return_value=json.loads(stdin_data),
                ):
                    update_mod.main()

        # Assert
        ctx = json.loads(_working_context_path(tmp_path).read_text(encoding="utf-8"))
        assert "src/foo.py" in ctx["modified_files"]

    def test_updates_modified_files_on_write(self, tmp_path: Path) -> None:
        # Arrange
        stdin_data = json.dumps(
            {
                "tool_name": "Write",
                "cwd": str(tmp_path),
                "tool_input": {"file_path": str(tmp_path / "src" / "bar.py")},
            }
        )

        # Act
        with patch.object(sys, "stdin", StringIO(stdin_data)):
            with patch.object(update_mod, "_CONTEXT_STORE_AVAILABLE", True):
                with patch.object(
                    update_mod,
                    "read_hook_input",
                    return_value=json.loads(stdin_data),
                ):
                    update_mod.main()

        # Assert
        ctx = json.loads(_working_context_path(tmp_path).read_text(encoding="utf-8"))
        assert "src/bar.py" in ctx["modified_files"]

    def test_excludes_claude_internal_files(self, tmp_path: Path) -> None:
        # Arrange
        stdin_data = json.dumps(
            {
                "tool_name": "Edit",
                "cwd": str(tmp_path),
                "tool_input": {"file_path": str(tmp_path / ".claude" / "Plans.md")},
            }
        )

        # Act
        with patch.object(sys, "stdin", StringIO(stdin_data)):
            with patch.object(update_mod, "_CONTEXT_STORE_AVAILABLE", True):
                with patch.object(
                    update_mod,
                    "read_hook_input",
                    return_value=json.loads(stdin_data),
                ):
                    update_mod.main()

        # Assert – .claude/ file is excluded; working-context not created
        assert not _working_context_path(tmp_path).exists()

    def test_does_nothing_for_non_edit_write_tool(self, tmp_path: Path) -> None:
        # Arrange
        stdin_data = json.dumps(
            {
                "tool_name": "Task",
                "cwd": str(tmp_path),
                "tool_input": {"subagent_type": "tester"},
            }
        )

        # Act
        with patch.object(sys, "stdin", StringIO(stdin_data)):
            with patch.object(update_mod, "_CONTEXT_STORE_AVAILABLE", True):
                with patch.object(
                    update_mod,
                    "read_hook_input",
                    return_value=json.loads(stdin_data),
                ):
                    update_mod.main()

        # Assert
        assert not _working_context_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# cleanup-session-context.py
# ---------------------------------------------------------------------------


class TestCleanupSessionContextMain:
    def test_removes_session_dir_and_working_context(self, tmp_path: Path) -> None:
        # Arrange
        context_store.init_context_dir(str(tmp_path))
        context_store.update_working_context(str(tmp_path), {"current_phase": "x"})
        assert _session_dir(tmp_path).is_dir()
        assert _working_context_path(tmp_path).is_file()

        stdin_data = json.dumps({"cwd": str(tmp_path)})

        # Act
        with patch.object(sys, "stdin", StringIO(stdin_data)):
            with patch.object(cleanup_mod, "_CONTEXT_STORE_AVAILABLE", True):
                cleanup_mod.main()

        # Assert
        assert not _session_dir(tmp_path).exists()
        assert not _working_context_path(tmp_path).exists()

    def test_does_not_raise_when_context_already_clean(self, tmp_path: Path) -> None:
        # Arrange
        stdin_data = json.dumps({"cwd": str(tmp_path)})

        # Act / Assert – no exception
        with patch.object(sys, "stdin", StringIO(stdin_data)):
            with patch.object(cleanup_mod, "_CONTEXT_STORE_AVAILABLE", True):
                cleanup_mod.main()


# ---------------------------------------------------------------------------
# tool_name="Agent" 互換テスト（Claude Code は "Agent" を送る）
# ---------------------------------------------------------------------------


class TestAgentToolNameCompat:
    """Claude Code は tool_name="Agent" を送る。"Task" からの移行互換を検証。"""

    def test_capture_writes_entry_for_agent_tool_name(self, tmp_path: Path) -> None:
        stdin_data = json.dumps(
            {
                "tool_name": "Agent",
                "cwd": str(tmp_path),
                "tool_input": {
                    "subagent_type": "tester",
                    "description": "Run tests",
                    "prompt": "pytest -q",
                },
                "tool_response": "All tests passed.",
            }
        )

        with patch.object(sys, "stdin", StringIO(stdin_data)):
            with patch.object(capture_mod, "_CONTEXT_STORE_AVAILABLE", True):
                capture_mod.main()

        entries = list(_entries_dir(tmp_path).glob("tester_*.json"))
        assert len(entries) == 1
        stored = json.loads(entries[0].read_text(encoding="utf-8"))
        assert stored["agent_id"] == "tester"
        assert stored["status"] == "done"

    def test_inject_appends_context_for_agent_tool_name(self, tmp_path: Path) -> None:
        context_store.write_entry(
            str(tmp_path),
            "tester",
            {
                "agent_id": "tester",
                "task_name": "run",
                "summary": "done",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "status": "done",
            },
        )
        stdin_data = json.dumps(
            {
                "tool_name": "Agent",
                "cwd": str(tmp_path),
                "tool_input": {"subagent_type": "backend-python-dev", "prompt": "implement X"},
            }
        )

        output_buf = StringIO()
        with patch.object(sys, "stdin", StringIO(stdin_data)):
            with patch.object(sys, "stdout", output_buf):
                with patch.object(inject_mod, "_CONTEXT_STORE_AVAILABLE", True):
                    inject_mod.main()

        output = output_buf.getvalue()
        assert output
        parsed = json.loads(output)
        hook_output = parsed["hookSpecificOutput"]
        assert "[Shared Context]" in hook_output["additionalContext"]
        assert "[Shared Context]" in hook_output["updatedInput"]["prompt"]
