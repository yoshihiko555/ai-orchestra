"""load-task-state.py の追加テスト — parse_tasks / format_summary / resolve_markers 等。"""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

import pytest

from tests.module_loader import load_module

_mod = load_module("load_task_state_ext", "packages/core/hooks/load-task-state.py")

parse_tasks = _mod.parse_tasks
format_summary = _mod.format_summary
resolve_markers = _mod.resolve_markers
build_marker_parser = _mod.build_marker_parser
DEFAULT_MARKERS = _mod.DEFAULT_MARKERS
DEFAULT_MARKER_PATTERN = _mod.DEFAULT_MARKER_PATTERN
DEFAULT_MARKER_TO_STATE = _mod.DEFAULT_MARKER_TO_STATE


# ---------------------------------------------------------------------------
# parse_tasks
# ---------------------------------------------------------------------------


class TestParseTasks:
    def test_parses_all_states(self) -> None:
        content = (
            "# Plans\n"
            "## Project: Test\n"
            "### Phase 1\n"
            "- `cc:done` completed task\n"
            "- `cc:WIP` working task\n"
            "- `cc:TODO` pending task\n"
            "- `cc:blocked` stuck task — 理由: waiting for API\n"
        )
        tasks = parse_tasks(content)

        assert len(tasks["done"]) == 1
        assert tasks["done"][0]["task"] == "completed task"
        assert len(tasks["WIP"]) == 1
        assert tasks["WIP"][0]["task"] == "working task"
        assert len(tasks["TODO"]) == 1
        assert tasks["TODO"][0]["task"] == "pending task"
        assert len(tasks["blocked"]) == 1
        assert tasks["blocked"][0]["task"] == "stuck task"
        assert tasks["blocked"][0]["reason"] == "waiting for API"

    def test_ignores_non_task_lines(self) -> None:
        content = (
            "# Plans\n"
            "## Project: Test\n"
            "### Phase 1\n"
            "Some description text\n"
            "#### Section heading\n"
            "- `cc:done` real task\n"
        )
        tasks = parse_tasks(content)
        assert len(tasks["done"]) == 1
        assert sum(len(v) for v in tasks.values()) == 1

    def test_skips_empty_task_name(self) -> None:
        content = "- `cc:done` \n- `cc:TODO` real task\n"
        tasks = parse_tasks(content)
        assert len(tasks["done"]) == 0
        assert len(tasks["TODO"]) == 1

    def test_blocked_without_reason(self) -> None:
        content = "- `cc:blocked` stuck without reason\n"
        tasks = parse_tasks(content)
        assert len(tasks["blocked"]) == 1
        assert tasks["blocked"][0]["reason"] is None

    def test_empty_content(self) -> None:
        tasks = parse_tasks("")
        assert all(len(v) == 0 for v in tasks.values())

    def test_no_markers_in_content(self) -> None:
        content = "# Plans\n- regular list item\n- another item\n"
        tasks = parse_tasks(content)
        assert all(len(v) == 0 for v in tasks.values())

    def test_custom_markers(self) -> None:
        custom_markers = {
            "todo": "TODO",
            "wip": "WIP",
            "done": "DONE",
            "blocked": "BLOCK",
        }
        pattern, to_state = build_marker_parser(custom_markers)
        content = "- `DONE` custom done\n- `WIP` custom wip\n"
        tasks = parse_tasks(content, pattern, to_state)
        assert len(tasks["done"]) == 1
        assert len(tasks["WIP"]) == 1


# ---------------------------------------------------------------------------
# format_summary
# ---------------------------------------------------------------------------


class TestFormatSummary:
    def test_shows_total_and_breakdown(self) -> None:
        tasks = {
            "done": [{"task": "a", "reason": None}],
            "WIP": [{"task": "b", "reason": None}],
            "TODO": [{"task": "c", "reason": None}],
            "blocked": [],
        }
        result = format_summary(tasks, max_display=20)
        assert "[task-memory] 3 tasks" in result
        assert "done: 1" in result
        assert "WIP: 1" in result
        assert "TODO: 1" in result

    def test_displays_wip_tasks(self) -> None:
        tasks = {
            "done": [],
            "WIP": [{"task": "working on X", "reason": None}],
            "TODO": [],
            "blocked": [],
        }
        result = format_summary(tasks, max_display=20)
        assert "WIP:" in result
        assert "working on X" in result

    def test_displays_next_todo(self) -> None:
        tasks = {
            "done": [],
            "WIP": [],
            "TODO": [{"task": "next task", "reason": None}],
            "blocked": [],
        }
        result = format_summary(tasks, max_display=20)
        assert "Next TODO:" in result
        assert "next task" in result

    def test_displays_blocked_with_reason(self) -> None:
        tasks = {
            "done": [],
            "WIP": [],
            "TODO": [],
            "blocked": [{"task": "stuck", "reason": "dependency missing"}],
        }
        result = format_summary(tasks, max_display=20)
        assert "Blocked:" in result
        assert "stuck" in result
        assert "dependency missing" in result

    def test_max_display_truncation(self) -> None:
        tasks = {
            "done": [],
            "WIP": [{"task": f"wip-{i}", "reason": None} for i in range(5)],
            "TODO": [{"task": f"todo-{i}", "reason": None} for i in range(5)],
            "blocked": [],
        }
        # max_display=3: WIP 3件表示 → 残り0件なので TODO 表示なし
        result = format_summary(tasks, max_display=3)
        assert "wip-0" in result
        assert "wip-2" in result
        assert "... and 2 more" in result

    def test_max_display_none_shows_all(self) -> None:
        tasks = {
            "done": [],
            "WIP": [{"task": f"wip-{i}", "reason": None} for i in range(10)],
            "TODO": [{"task": f"todo-{i}", "reason": None} for i in range(10)],
            "blocked": [],
        }
        result = format_summary(tasks, max_display=None)
        for i in range(10):
            assert f"wip-{i}" in result
            assert f"todo-{i}" in result

    def test_blocked_omitted_message_when_over_limit(self) -> None:
        tasks = {
            "done": [],
            "WIP": [{"task": f"wip-{i}", "reason": None} for i in range(3)],
            "TODO": [],
            "blocked": [{"task": "b", "reason": None}],
        }
        # max_display=3: WIP で枠を使い切り、blocked は省略メッセージ
        result = format_summary(tasks, max_display=3)
        assert "1 件省略" in result


# ---------------------------------------------------------------------------
# resolve_markers
# ---------------------------------------------------------------------------


class TestResolveMarkers:
    def test_returns_defaults_when_no_markers_in_config(self) -> None:
        result = resolve_markers({})
        assert result == DEFAULT_MARKERS

    def test_overrides_specific_markers(self) -> None:
        config = {"markers": {"todo": "my:TODO", "done": "my:done"}}
        result = resolve_markers(config)
        assert result["todo"] == "my:TODO"
        assert result["done"] == "my:done"
        assert result["wip"] == "cc:WIP"  # デフォルト維持
        assert result["blocked"] == "cc:blocked"  # デフォルト維持

    def test_ignores_empty_marker_values(self) -> None:
        config = {"markers": {"todo": "", "wip": "  "}}
        result = resolve_markers(config)
        assert result["todo"] == "cc:TODO"  # デフォルトにフォールバック
        assert result["wip"] == "cc:WIP"

    def test_ignores_non_dict_markers(self) -> None:
        config = {"markers": "invalid"}
        result = resolve_markers(config)
        assert result == DEFAULT_MARKERS


# ---------------------------------------------------------------------------
# build_marker_parser
# ---------------------------------------------------------------------------


class TestBuildMarkerParser:
    def test_raises_on_duplicate_markers_strict(self) -> None:
        markers = {"todo": "same", "wip": "same", "done": "done", "blocked": "blocked"}
        with pytest.raises(ValueError, match="assigned to both"):
            build_marker_parser(markers, strict=True)

    def test_skips_duplicate_markers_non_strict(self) -> None:
        markers = {"todo": "same", "wip": "same", "done": "done", "blocked": "blocked"}
        pattern, to_state = build_marker_parser(markers, strict=False)
        # 重複は最初のものだけ登録される
        assert to_state["same"] == "TODO"

    def test_generates_working_pattern(self) -> None:
        pattern, to_state = build_marker_parser(DEFAULT_MARKERS)

        match = pattern.search("- `cc:WIP` some task")
        assert match is not None
        assert to_state[match.group(1)] == "WIP"


# ---------------------------------------------------------------------------
# main — 追加シナリオ
# ---------------------------------------------------------------------------


class TestMainAdditionalScenarios:
    def test_no_plans_file_exits_silently(self, tmp_path: Path, monkeypatch) -> None:
        """Plans.md が存在しない場合は何も出力しない。"""
        monkeypatch.setattr(_mod, "read_hook_input", lambda: {"cwd": str(tmp_path)})
        monkeypatch.setattr(
            _mod,
            "load_config",
            lambda _: {
                "plans_file": ".claude/Plans.md",
                "show_summary_on_start": True,
                "max_display_tasks": 20,
                "markers": dict(DEFAULT_MARKERS),
            },
        )
        out = StringIO()
        monkeypatch.setattr(sys, "stdout", out)

        _mod.main()
        assert out.getvalue() == ""

    def test_empty_plans_file_exits_silently(self, tmp_path: Path, monkeypatch) -> None:
        """Plans.md が空の場合は何も出力しない。"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text("", encoding="utf-8")

        monkeypatch.setattr(_mod, "read_hook_input", lambda: {"cwd": str(tmp_path)})
        monkeypatch.setattr(
            _mod,
            "load_config",
            lambda _: {
                "plans_file": ".claude/Plans.md",
                "show_summary_on_start": True,
                "max_display_tasks": 20,
                "markers": dict(DEFAULT_MARKERS),
            },
        )
        out = StringIO()
        monkeypatch.setattr(sys, "stdout", out)

        _mod.main()
        assert out.getvalue() == ""

    def test_summary_output_with_tasks(self, tmp_path: Path, monkeypatch) -> None:
        """タスクがある場合にサマリーが出力される。"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n"
            "## Project: Test\n"
            "### Phase 1: Dev `cc:WIP`\n"
            "- `cc:done` setup\n"
            "- `cc:WIP` implement feature\n"
            "- `cc:TODO` write tests\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(_mod, "read_hook_input", lambda: {"cwd": str(tmp_path)})
        monkeypatch.setattr(
            _mod,
            "load_config",
            lambda _: {
                "plans_file": ".claude/Plans.md",
                "show_summary_on_start": True,
                "max_display_tasks": 20,
                "markers": dict(DEFAULT_MARKERS),
            },
        )
        out = StringIO()
        monkeypatch.setattr(sys, "stdout", out)

        _mod.main()

        output = out.getvalue()
        assert "[task-memory]" in output
        assert "implement feature" in output
        assert "write tests" in output

    def test_no_tasks_in_plans_exits_silently(self, tmp_path: Path, monkeypatch) -> None:
        """Plans.md にマーカー付きタスクがない場合は何も出力しない。"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n## Project: Test\n### Phase 1\nSome notes\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(_mod, "read_hook_input", lambda: {"cwd": str(tmp_path)})
        monkeypatch.setattr(
            _mod,
            "load_config",
            lambda _: {
                "plans_file": ".claude/Plans.md",
                "show_summary_on_start": True,
                "max_display_tasks": 20,
                "markers": dict(DEFAULT_MARKERS),
            },
        )
        out = StringIO()
        monkeypatch.setattr(sys, "stdout", out)

        _mod.main()
        assert out.getvalue() == ""

    def test_invalid_markers_falls_back_to_defaults(self, tmp_path: Path, monkeypatch) -> None:
        """重複マーカー設定時にデフォルトにフォールバックする。"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n## Project: Test\n### Phase 1\n- `cc:TODO` task\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(_mod, "read_hook_input", lambda: {"cwd": str(tmp_path)})
        monkeypatch.setattr(
            _mod,
            "load_config",
            lambda _: {
                "plans_file": ".claude/Plans.md",
                "show_summary_on_start": True,
                "max_display_tasks": 20,
                "markers": {"todo": "dup", "wip": "dup", "done": "done", "blocked": "blocked"},
            },
        )
        out = StringIO()
        err = StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)

        _mod.main()

        assert "fallback to defaults" in err.getvalue()
        # フォールバック後もタスクが表示される
        assert "[task-memory]" in out.getvalue()
