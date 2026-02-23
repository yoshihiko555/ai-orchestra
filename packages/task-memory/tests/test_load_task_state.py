from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_module(module_name: str, relative_path: str):
    module_path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


load_task_state = load_module("task_memory_load_task_state", "packages/task-memory/hooks/load-task-state.py")


def test_load_config_uses_project_override_without_ai_orchestra_dir(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)

    config_dir = tmp_path / ".claude" / "config" / "task-memory"
    config_dir.mkdir(parents=True)
    (config_dir / "task-memory.yaml").write_text(
        'plans_file: ".claude/MyPlans.md"\nshow_summary_on_start: false\nmax_display_tasks: 7\n',
        encoding="utf-8",
    )

    config = load_task_state.load_config(str(tmp_path))

    assert config == {
        "plans_file": ".claude/MyPlans.md",
        "show_summary_on_start": False,
        "max_display_tasks": 7,
    }


def test_load_config_returns_defaults_when_config_not_found(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)

    config = load_task_state.load_config(str(tmp_path))

    assert config == {
        "plans_file": ".claude/Plans.md",
        "show_summary_on_start": True,
        "max_display_tasks": 20,
        "markers": {
            "todo": "cc:TODO",
            "wip": "cc:WIP",
            "done": "cc:done",
            "blocked": "cc:blocked",
        },
    }


def test_load_config_falls_back_to_repo_hook_common_when_orchestra_dir_is_invalid(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path / "missing-orchestra"))

    config_dir = tmp_path / ".claude" / "config" / "task-memory"
    config_dir.mkdir(parents=True)
    (config_dir / "task-memory.yaml").write_text(
        'plans_file: ".claude/PlanB.md"\nshow_summary_on_start: true\nmax_display_tasks: 3\n',
        encoding="utf-8",
    )

    config = load_task_state.load_config(str(tmp_path))

    assert config["plans_file"] == ".claude/PlanB.md"
    assert config["show_summary_on_start"] is True
    assert config["max_display_tasks"] == 3


def test_resolve_markers_falls_back_to_defaults_for_missing_or_invalid_values() -> None:
    markers = load_task_state.resolve_markers(
        {"markers": {"todo": "todo!", "wip": "", "done": None, "blocked": "blocked!"}}
    )

    assert markers == {
        "todo": "todo!",
        "wip": "cc:WIP",
        "done": "cc:done",
        "blocked": "blocked!",
    }


def test_parse_tasks_supports_custom_markers() -> None:
    markers = load_task_state.resolve_markers(
        {
            "markers": {
                "todo": "task:todo",
                "wip": "task:wip",
                "done": "task:done",
                "blocked": "task:blocked",
            }
        }
    )
    marker_pattern, marker_to_state = load_task_state.build_marker_parser(markers)
    content = "\n".join(
        [
            "- `task:wip` 実装中",
            "- `task:todo` テスト作成",
            "- `task:done` 設計完了",
            "- `task:blocked` 連携待ち — 理由: 外部API未提供",
        ]
    )

    tasks = load_task_state.parse_tasks(content, marker_pattern, marker_to_state)

    assert tasks["WIP"] == [{"task": "実装中", "reason": None}]
    assert tasks["TODO"] == [{"task": "テスト作成", "reason": None}]
    assert tasks["done"] == [{"task": "設計完了", "reason": None}]
    assert tasks["blocked"] == [{"task": "連携待ち", "reason": "外部API未提供"}]


def test_build_marker_parser_raises_for_duplicate_markers() -> None:
    markers = {
        "todo": "task:shared",
        "wip": "task:shared",
        "done": "task:done",
        "blocked": "task:blocked",
    }

    with pytest.raises(ValueError, match="assigned to both"):
        load_task_state.build_marker_parser(markers, strict=True)


def test_parse_tasks_extracts_each_state_and_blocked_reason() -> None:
    content = "\n".join(
        [
            "- `cc:WIP` 仕様確認",
            "- `cc:TODO` テスト追加",
            "- `cc:done` ドキュメント更新",
            "- `cc:blocked` API 実装待ち — 理由: 依存先の対応待ち",
        ]
    )

    tasks = load_task_state.parse_tasks(content)

    assert tasks["WIP"] == [{"task": "仕様確認", "reason": None}]
    assert tasks["TODO"] == [{"task": "テスト追加", "reason": None}]
    assert tasks["done"] == [{"task": "ドキュメント更新", "reason": None}]
    assert tasks["blocked"] == [{"task": "API 実装待ち", "reason": "依存先の対応待ち"}]


def test_parse_tasks_ignores_non_list_lines_and_unknown_markers() -> None:
    content = "\n".join(
        [
            "`cc:TODO` 箇条書きでない行",
            "* `cc:WIP` アスタリスク行",
            "- `cc:todo` 小文字マーカー",
            "- `cc:TODOX` 未定義マーカー",
        ]
    )

    tasks = load_task_state.parse_tasks(content)

    assert tasks == {"WIP": [], "TODO": [], "done": [], "blocked": []}


def test_parse_tasks_skips_entries_with_empty_task_text() -> None:
    content = "\n".join(
        [
            "- `cc:TODO`",
            "- `cc:blocked` — 理由: 外部調整待ち",
            "- `cc:WIP`   実装する",
        ]
    )

    tasks = load_task_state.parse_tasks(content)

    assert tasks["TODO"] == []
    assert tasks["blocked"] == []
    assert tasks["WIP"] == [{"task": "実装する", "reason": None}]


def test_format_summary_limited_uses_total_cap_and_prioritizes_todo_over_blocked() -> None:
    tasks = {
        "WIP": [{"task": "w1", "reason": None}, {"task": "w2", "reason": None}],
        "TODO": [
            {"task": "t1", "reason": None},
            {"task": "t2", "reason": None},
            {"task": "t3", "reason": None},
            {"task": "t4", "reason": None},
            {"task": "t5", "reason": None},
        ],
        "done": [{"task": "d1", "reason": None}],
        "blocked": [{"task": "b1", "reason": "確認待ち"}, {"task": "b2", "reason": None}],
    }

    summary = load_task_state.format_summary(tasks, max_display=4)

    assert "[task-memory] 10 tasks (done: 1, WIP: 2, TODO: 5, blocked: 2)" in summary
    assert "  WIP:\n    - w1\n    - w2" in summary
    assert "  Next TODO:\n    - t1\n    - t2" in summary
    assert "    ... and 3 more" in summary
    assert "  Blocked: (上限のため 2 件省略)" in summary
    assert summary.count("\n    - ") == 4


def test_format_summary_limited_shows_blocked_when_budget_remains() -> None:
    tasks = {
        "WIP": [{"task": "w1", "reason": None}],
        "TODO": [],
        "done": [],
        "blocked": [{"task": "b1", "reason": "確認待ち"}, {"task": "b2", "reason": None}],
    }

    summary = load_task_state.format_summary(tasks, max_display=2)

    assert "  WIP:\n    - w1" in summary
    assert "  Blocked:\n    - b1 (理由: 確認待ち)" in summary
    assert "    ... and 1 more" in summary


def test_format_summary_unlimited_does_not_emit_truncation_line() -> None:
    tasks = {
        "WIP": [],
        "TODO": [
            {"task": "t1", "reason": None},
            {"task": "t2", "reason": None},
            {"task": "t3", "reason": None},
            {"task": "t4", "reason": None},
        ],
        "done": [],
        "blocked": [],
    }

    summary = load_task_state.format_summary(tasks, max_display=None)

    assert "  Next TODO:\n    - t1\n    - t2\n    - t3\n    - t4" in summary
    assert "... and" not in summary


def test_main_uses_unlimited_when_configured_max_display_is_zero(
    tmp_path, monkeypatch
) -> None:
    plans_path = tmp_path / ".claude" / "Plans.md"
    plans_path.parent.mkdir(parents=True)
    plans_path.write_text("- `cc:TODO` task", encoding="utf-8")

    monkeypatch.setattr(load_task_state, "read_hook_input", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(
        load_task_state,
        "load_config",
        lambda _project_dir: {
            "plans_file": ".claude/Plans.md",
            "show_summary_on_start": True,
            "max_display_tasks": 0,
        },
    )
    monkeypatch.setattr(
        load_task_state,
        "parse_tasks",
        lambda _content, *_args: {
            "WIP": [],
            "TODO": [{"task": "task", "reason": None}],
            "done": [],
            "blocked": [],
        },
    )

    called = {"max_display": "unset"}
    monkeypatch.setattr(
        load_task_state,
        "format_summary",
        lambda _tasks, max_display: called.update({"max_display": max_display}) or "summary",
    )
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda message: printed.append(message))

    load_task_state.main()

    assert called["max_display"] is None
    assert printed == ["summary"]


def test_main_falls_back_to_default_max_display_for_invalid_value(
    tmp_path, monkeypatch
) -> None:
    plans_path = tmp_path / ".claude" / "Plans.md"
    plans_path.parent.mkdir(parents=True)
    plans_path.write_text("- `cc:TODO` task", encoding="utf-8")

    monkeypatch.setattr(load_task_state, "read_hook_input", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(
        load_task_state,
        "load_config",
        lambda _project_dir: {
            "plans_file": ".claude/Plans.md",
            "show_summary_on_start": True,
            "max_display_tasks": "invalid",
        },
    )
    monkeypatch.setattr(
        load_task_state,
        "parse_tasks",
        lambda _content, *_args: {
            "WIP": [],
            "TODO": [{"task": "task", "reason": None}],
            "done": [],
            "blocked": [],
        },
    )

    called = {"max_display": "unset"}
    monkeypatch.setattr(
        load_task_state,
        "format_summary",
        lambda _tasks, max_display: called.update({"max_display": max_display}) or "summary",
    )
    monkeypatch.setattr("builtins.print", lambda _message: None)

    load_task_state.main()

    assert called["max_display"] == 20


def test_main_treats_string_zero_max_display_as_unlimited(tmp_path, monkeypatch) -> None:
    plans_path = tmp_path / ".claude" / "Plans.md"
    plans_path.parent.mkdir(parents=True)
    plans_path.write_text("- `cc:TODO` task", encoding="utf-8")

    monkeypatch.setattr(load_task_state, "read_hook_input", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(
        load_task_state,
        "load_config",
        lambda _project_dir: {
            "plans_file": ".claude/Plans.md",
            "show_summary_on_start": True,
            "max_display_tasks": "0",
            "markers": {"todo": "cc:TODO", "wip": "cc:WIP", "done": "cc:done", "blocked": "cc:blocked"},
        },
    )
    monkeypatch.setattr(
        load_task_state,
        "parse_tasks",
        lambda _content, *_args: {
            "WIP": [],
            "TODO": [{"task": "task", "reason": None}],
            "done": [],
            "blocked": [],
        },
    )

    called = {"max_display": "unset"}
    monkeypatch.setattr(
        load_task_state,
        "format_summary",
        lambda _tasks, max_display: called.update({"max_display": max_display}) or "summary",
    )
    monkeypatch.setattr("builtins.print", lambda _message: None)

    load_task_state.main()

    assert called["max_display"] is None


def test_main_passes_custom_marker_mapping_to_parse_tasks(tmp_path, monkeypatch) -> None:
    plans_path = tmp_path / ".claude" / "Plans.md"
    plans_path.parent.mkdir(parents=True)
    plans_path.write_text("- `x:todo` task", encoding="utf-8")

    monkeypatch.setattr(load_task_state, "read_hook_input", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(
        load_task_state,
        "load_config",
        lambda _project_dir: {
            "plans_file": ".claude/Plans.md",
            "show_summary_on_start": True,
            "max_display_tasks": 1,
            "markers": {"todo": "x:todo", "wip": "x:wip", "done": "x:done", "blocked": "x:blocked"},
        },
    )

    captured: dict[str, dict[str, str]] = {}

    def fake_parse_tasks(_content, _marker_pattern, marker_to_state):
        captured["marker_to_state"] = marker_to_state
        return {"WIP": [], "TODO": [{"task": "task", "reason": None}], "done": [], "blocked": []}

    monkeypatch.setattr(load_task_state, "parse_tasks", fake_parse_tasks)
    monkeypatch.setattr(load_task_state, "format_summary", lambda _tasks, _max: "summary")
    monkeypatch.setattr("builtins.print", lambda _message: None)

    load_task_state.main()

    assert captured["marker_to_state"] == {
        "x:todo": "TODO",
        "x:wip": "WIP",
        "x:done": "done",
        "x:blocked": "blocked",
    }


def test_main_falls_back_to_default_markers_when_duplicates_exist(
    tmp_path, monkeypatch
) -> None:
    plans_path = tmp_path / ".claude" / "Plans.md"
    plans_path.parent.mkdir(parents=True)
    plans_path.write_text("- `dup:task` task", encoding="utf-8")

    monkeypatch.setattr(load_task_state, "read_hook_input", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(
        load_task_state,
        "load_config",
        lambda _project_dir: {
            "plans_file": ".claude/Plans.md",
            "show_summary_on_start": True,
            "max_display_tasks": 1,
            "markers": {
                "todo": "dup:task",
                "wip": "dup:task",
                "done": "dup:done",
                "blocked": "dup:blocked",
            },
        },
    )

    captured: dict[str, dict[str, str]] = {}

    def fake_parse_tasks(_content, _marker_pattern, marker_to_state):
        captured["marker_to_state"] = marker_to_state
        return {"WIP": [], "TODO": [{"task": "task", "reason": None}], "done": [], "blocked": []}

    monkeypatch.setattr(load_task_state, "parse_tasks", fake_parse_tasks)
    monkeypatch.setattr(load_task_state, "format_summary", lambda _tasks, _max: "summary")
    monkeypatch.setattr("builtins.print", lambda _message, **_kwargs: None)

    load_task_state.main()

    assert captured["marker_to_state"] == load_task_state.DEFAULT_MARKER_TO_STATE
