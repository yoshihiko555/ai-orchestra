from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_module(module_name: str, relative_path: str):
    module_path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


load_task_state = load_module("core_load_task_state", "packages/core/hooks/load-task-state.py")


def test_load_config_uses_project_override_without_ai_orchestra_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)

    config_dir = tmp_path / ".claude" / "config" / "core"
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

    config_dir = tmp_path / ".claude" / "config" / "core"
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


def test_main_uses_unlimited_when_configured_max_display_is_zero(tmp_path, monkeypatch) -> None:
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
        lambda _tasks, max_display, **_kwargs: (
            called.update({"max_display": max_display}) or "summary"
        ),
    )
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda message: printed.append(message))

    load_task_state.main()

    assert called["max_display"] is None
    assert printed == ["summary"]


def test_main_prints_summary_when_only_orders_exist_and_no_tasks(tmp_path, monkeypatch) -> None:
    plans_path = tmp_path / ".claude" / "Plans.md"
    plans_path.parent.mkdir(parents=True)
    plans_path.write_text(
        """# Plans

## Project: HeadingOnly

#### Goal
- Ship it later

### Phase 1: Setup `cc:TODO`
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(load_task_state, "read_hook_input", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(
        load_task_state,
        "load_config",
        lambda _project_dir: {
            "plans_file": ".claude/Plans.md",
            "show_summary_on_start": True,
            "max_display_tasks": 20,
        },
    )
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda message: printed.append(message))

    load_task_state.main()

    assert printed
    assert "Goal: Ship it later" in printed[0]


def test_main_falls_back_to_default_max_display_for_invalid_value(tmp_path, monkeypatch) -> None:
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
        lambda _tasks, max_display, **_kwargs: (
            called.update({"max_display": max_display}) or "summary"
        ),
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
            "markers": {
                "todo": "cc:TODO",
                "wip": "cc:WIP",
                "done": "cc:done",
                "blocked": "cc:blocked",
            },
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
        lambda _tasks, max_display, **_kwargs: (
            called.update({"max_display": max_display}) or "summary"
        ),
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
    monkeypatch.setattr(
        load_task_state, "format_summary", lambda _tasks, _max, **_kwargs: "summary"
    )
    monkeypatch.setattr("builtins.print", lambda _message: None)

    load_task_state.main()

    assert captured["marker_to_state"] == {
        "x:todo": "TODO",
        "x:wip": "WIP",
        "x:done": "done",
        "x:blocked": "blocked",
    }


def test_detect_completed_projects_blocks_on_unchecked_acceptance_criteria() -> None:
    content = "\n".join(
        [
            "## Project: Demo",
            "### Phase 1: Setup",
            "#### Tasks",
            "- `cc:done` task A",
            "#### Acceptance Criteria",
            "- [ ] condition — verify: `echo ok`",
        ]
    )

    completed = load_task_state.detect_completed_projects(
        content, load_task_state.DEFAULT_MARKER_PATTERN, load_task_state.DEFAULT_MARKER_TO_STATE
    )

    assert completed == []


def test_detect_completed_projects_blocks_even_when_phase_header_marked_done() -> None:
    content = "\n".join(
        [
            "## Project: Demo",
            "### Phase 1: Setup `cc:done`",
            "#### Acceptance Criteria",
            "- [ ] condition — verify: `echo ok`",
        ]
    )

    completed = load_task_state.detect_completed_projects(
        content, load_task_state.DEFAULT_MARKER_PATTERN, load_task_state.DEFAULT_MARKER_TO_STATE
    )

    assert completed == []


def test_detect_completed_projects_archives_when_all_acceptance_criteria_checked() -> None:
    content = "\n".join(
        [
            "## Project: Demo",
            "### Phase 1: Setup",
            "#### Tasks",
            "- `cc:done` task A",
            "#### Acceptance Criteria",
            "- [x] condition1 — verify: `echo 1`",
            "- [X] condition2 — verify: `echo 2`",
        ]
    )

    completed = load_task_state.detect_completed_projects(
        content, load_task_state.DEFAULT_MARKER_PATTERN, load_task_state.DEFAULT_MARKER_TO_STATE
    )

    assert len(completed) == 1
    assert completed[0]["name"] == "Demo"


def test_detect_completed_projects_header_done_with_ac_blocks_on_residual_task() -> None:
    content = "\n".join(
        [
            "## Project: Demo",
            "### Phase 1: Setup `cc:done`",
            "#### Tasks",
            "- `cc:TODO` remaining task",
            "#### Acceptance Criteria",
            "- [x] condition1 — verify: `echo 1`",
            "- [X] condition2 — verify: `echo 2`",
        ]
    )

    completed = load_task_state.detect_completed_projects(
        content, load_task_state.DEFAULT_MARKER_PATTERN, load_task_state.DEFAULT_MARKER_TO_STATE
    )

    assert completed == []


def test_detect_completed_projects_header_done_with_ac_and_no_task_lines_archives() -> None:
    content = "\n".join(
        [
            "## Project: Demo",
            "### Phase 1: Setup `cc:done`",
            "#### Acceptance Criteria",
            "- [x] condition1 — verify: `echo 1`",
            "- [X] condition2 — verify: `echo 2`",
        ]
    )

    completed = load_task_state.detect_completed_projects(
        content, load_task_state.DEFAULT_MARKER_PATTERN, load_task_state.DEFAULT_MARKER_TO_STATE
    )

    assert len(completed) == 1
    assert completed[0]["name"] == "Demo"


def test_detect_completed_projects_header_done_without_ac_section_legacy_archives() -> None:
    content = "\n".join(
        [
            "## Project: Demo",
            "### Phase 1: Setup `cc:done`",
            "#### Tasks",
            "- `cc:TODO` remaining task",
        ]
    )

    completed = load_task_state.detect_completed_projects(
        content, load_task_state.DEFAULT_MARKER_PATTERN, load_task_state.DEFAULT_MARKER_TO_STATE
    )

    # 後方互換: AC セクションがなければ従来通り見出し cc:done で短絡し、body は見ない
    assert len(completed) == 1
    assert completed[0]["name"] == "Demo"


def test_parse_tasks_skips_ac_checkbox_line_even_with_marker_like_text_in_body() -> None:
    content = "\n".join(
        [
            "### Phase 1: Setup",
            "#### Tasks",
            "- `cc:done` task A",
            "#### Acceptance Criteria",
            "- [ ] no remaining `cc:TODO` tasks — judge: manual check",
        ]
    )

    tasks = load_task_state.parse_tasks(content)

    assert tasks["done"] == [{"task": "task A", "reason": None}]
    assert tasks["TODO"] == []


def test_detect_completed_projects_does_not_misclassify_markdown_link_bullet_as_checked() -> None:
    content = "\n".join(
        [
            "## Project: Demo",
            "### Phase 1: Setup",
            "#### Tasks",
            "- `cc:done` task A",
            "- [See discussion](https://example.com/issue/1)",
        ]
    )

    completed = load_task_state.detect_completed_projects(
        content, load_task_state.DEFAULT_MARKER_PATTERN, load_task_state.DEFAULT_MARKER_TO_STATE
    )

    assert completed == []


def test_detect_completed_projects_empty_acceptance_criteria_section_does_not_block() -> None:
    content = "\n".join(
        [
            "## Project: Demo",
            "### Phase 1: Setup",
            "#### Tasks",
            "- `cc:done` task A",
            "#### Acceptance Criteria",
        ]
    )

    completed = load_task_state.detect_completed_projects(
        content, load_task_state.DEFAULT_MARKER_PATTERN, load_task_state.DEFAULT_MARKER_TO_STATE
    )

    assert len(completed) == 1
    assert completed[0]["name"] == "Demo"


def test_detect_completed_projects_checkbox_outside_ac_section_uses_legacy_marker_check() -> None:
    content = "\n".join(
        [
            "## Project: Demo",
            "### Phase 1: Setup",
            "#### Tasks",
            "- `cc:done` task A",
            "- [ ] not an AC line since there is no Acceptance Criteria heading",
        ]
    )

    completed = load_task_state.detect_completed_projects(
        content, load_task_state.DEFAULT_MARKER_PATTERN, load_task_state.DEFAULT_MARKER_TO_STATE
    )

    # AC セクション外の `- [ ]` は特別扱いせず、cc: マーカーなし行として未完了扱いになる
    assert completed == []


def test_parse_tasks_never_includes_acceptance_criteria_lines() -> None:
    content = "\n".join(
        [
            "- `cc:done` task A",
            "- [ ] condition — verify: `echo ok`",
            "- [x] condition2 — verify: `echo done`",
            "- `cc:TODO` task B",
        ]
    )

    tasks = load_task_state.parse_tasks(content)

    assert tasks["done"] == [{"task": "task A", "reason": None}]
    assert tasks["TODO"] == [{"task": "task B", "reason": None}]
    all_task_texts = [item["task"] for items in tasks.values() for item in items]
    assert not any("condition" in text for text in all_task_texts)


def test_detect_completed_projects_legacy_format_without_acceptance_criteria() -> None:
    completed_content = "\n".join(
        [
            "## Project: Legacy",
            "### Phase 1: Setup",
            "- `cc:done` task A",
        ]
    )
    incomplete_content = "\n".join(
        [
            "## Project: Legacy",
            "### Phase 1: Setup",
            "- `cc:done` task A",
            "- `cc:TODO` task B",
        ]
    )

    completed = load_task_state.detect_completed_projects(
        completed_content,
        load_task_state.DEFAULT_MARKER_PATTERN,
        load_task_state.DEFAULT_MARKER_TO_STATE,
    )
    incomplete = load_task_state.detect_completed_projects(
        incomplete_content,
        load_task_state.DEFAULT_MARKER_PATTERN,
        load_task_state.DEFAULT_MARKER_TO_STATE,
    )

    assert len(completed) == 1
    assert incomplete == []


def test_main_falls_back_to_default_markers_when_duplicates_exist(tmp_path, monkeypatch) -> None:
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
    monkeypatch.setattr(
        load_task_state, "format_summary", lambda _tasks, _max, **_kwargs: "summary"
    )
    monkeypatch.setattr("builtins.print", lambda _message, **_kwargs: None)

    load_task_state.main()

    assert captured["marker_to_state"] == load_task_state.DEFAULT_MARKER_TO_STATE


def test_parse_orders_and_summary_include_single_project_order() -> None:
    content = """# Plans

## Project: Launch

#### Goal

- Ship v2

#### Open Questions

- Which region launches first?
- Is migration downtime acceptable?

### Phase 1: Setup `cc:WIP`

#### Tasks

- `cc:WIP` Do the thing
"""

    orders = load_task_state.parse_orders(content)
    tasks = load_task_state.parse_tasks(content)
    summary = load_task_state.format_summary(tasks, 20, orders=orders)

    assert orders == [{"name": "Launch", "goal": "Ship v2", "open_questions": 2}]
    assert summary.splitlines()[:4] == [
        "[task-memory] 1 tasks (WIP: 1)",
        "  Goal: Ship v2",
        "  Open Questions: 2",
        "  WIP:",
    ]


def test_summary_qualifies_order_labels_for_multiple_projects() -> None:
    content = """# Plans

## Project: Alpha

#### Goal
- Ship Alpha

#### Open Questions
- Alpha question?

### Phase 1: Build `cc:TODO`
#### Tasks
- `cc:TODO` Alpha task

## Project: Beta

#### Goal
- Ship Beta

#### Open Questions
- Beta question one?
- Beta question two?

### Phase 1: Build `cc:TODO`
#### Tasks
- `cc:TODO` Beta task
"""

    orders = load_task_state.parse_orders(content)
    summary = load_task_state.format_summary(
        load_task_state.parse_tasks(content), 20, orders=orders
    )

    assert orders == [
        {"name": "Alpha", "goal": "Ship Alpha", "open_questions": 1},
        {"name": "Beta", "goal": "Ship Beta", "open_questions": 2},
    ]
    assert summary.splitlines()[1:5] == [
        "  Goal (Alpha): Ship Alpha",
        "  Goal (Beta): Ship Beta",
        "  Open Questions (Alpha): 1",
        "  Open Questions (Beta): 2",
    ]


def test_summary_qualifies_goal_label_even_when_only_one_project_has_a_goal() -> None:
    content = """# Plans

## Project: Alpha

#### Goal
- Ship Alpha

### Phase 1: Build `cc:TODO`
#### Tasks
- `cc:TODO` Alpha task

## Project: Beta

### Phase 1: Build `cc:TODO`
#### Tasks
- `cc:TODO` Beta task
"""

    orders = load_task_state.parse_orders(content)
    summary = load_task_state.format_summary(
        load_task_state.parse_tasks(content), 20, orders=orders
    )

    assert "  Goal (Alpha): Ship Alpha" in summary.splitlines()
    assert "  Goal: Ship Alpha" not in summary.splitlines()


def test_parse_orders_preserves_duplicate_project_names_as_separate_entries() -> None:
    content = """# Plans

## Project: Dup

#### Goal
- First goal

### Phase 1: Build `cc:TODO`
#### Tasks
- `cc:TODO` task one

## Project: Dup

#### Goal
- Second goal

### Phase 1: Build `cc:TODO`
#### Tasks
- `cc:TODO` task two
"""

    orders = load_task_state.parse_orders(content)

    assert orders == [
        {"name": "Dup", "goal": "First goal", "open_questions": 0},
        {"name": "Dup", "goal": "Second goal", "open_questions": 0},
    ]
    summary = load_task_state.format_summary(
        load_task_state.parse_tasks(content), 20, orders=orders
    )
    assert "  Goal (Dup): First goal" in summary.splitlines()
    assert "  Goal (Dup): Second goal" in summary.splitlines()


def test_parse_orders_ignores_none_open_question_bullets() -> None:
    content = """# Plans

## Project: Settled

#### Open Questions
- なし
- N/A.

### Phase 1: Build `cc:TODO`
#### Tasks
- `cc:TODO` Start work
"""

    orders = load_task_state.parse_orders(content)
    summary = load_task_state.format_summary(
        load_task_state.parse_tasks(content), 20, orders=orders
    )

    assert orders == [{"name": "Settled", "goal": None, "open_questions": 0}]
    assert "Open Questions" not in summary


def test_parse_orders_skips_placeholder_goal_and_open_question_bullets() -> None:
    content = """# Plans

## Project: Placeholder Only

#### Goal
- {目的。何のために、誰の何が変わるか}

#### Open Questions
- {未決事項。決まったら Decisions へ移して消す}
- Should X happen?

## Project: Real Goal

#### Goal
- {目的。何のために、誰の何が変わるか}
- Ship the real change
"""

    orders = load_task_state.parse_orders(content)
    summary = load_task_state.format_summary(
        load_task_state.parse_tasks(content), 20, orders=orders
    )

    assert orders[0] == {"name": "Placeholder Only", "goal": None, "open_questions": 1}
    assert orders[1]["goal"] == "Ship the real change"
    assert "{目的" not in summary


def test_parse_tasks_skips_cc_marker_inside_order_context() -> None:
    content = """# Plans

## Project: Test

#### Context
- `cc:TODO` some context note

### Phase 1: Build `cc:TODO`
#### Tasks
- `cc:TODO` Real task
"""

    tasks = load_task_state.parse_tasks(content)

    assert tasks["TODO"] == [{"task": "Real task", "reason": None}]


def test_fenced_code_block_inside_order_section_is_not_treated_as_structure() -> None:
    content = """# Plans

## Project: Test

#### Context

```
### Phase 9: Fake `cc:WIP`
#### Tasks
- `cc:TODO` fake task
```

- Real context note

#### Open Questions

- Real question?

### Phase 1: Build `cc:TODO`

#### Tasks

- `cc:TODO` Real task
"""

    orders = load_task_state.parse_orders(content)
    tasks = load_task_state.parse_tasks(content)

    assert orders == [{"name": "Test", "goal": None, "open_questions": 1}]
    assert tasks["TODO"] == [{"task": "Real task", "reason": None}]
    assert tasks["WIP"] == []


def test_legacy_plans_remain_byte_for_byte_compatible() -> None:
    content = """# Plans

## Project: Legacy

### Phase 1: Setup `cc:WIP`

#### Tasks

- `cc:WIP` Do the thing
- `cc:TODO` Do next thing
- `cc:blocked` Blocked thing — 理由: waiting
"""
    expected_tasks = {
        "WIP": [{"task": "Do the thing", "reason": None}],
        "TODO": [{"task": "Do next thing", "reason": None}],
        "done": [],
        "blocked": [{"task": "Blocked thing", "reason": "waiting"}],
    }
    expected_summary = (
        "[task-memory] 3 tasks (WIP: 1, TODO: 1, blocked: 1)\n"
        "  WIP:\n"
        "    - Do the thing\n"
        "  Next TODO:\n"
        "    - Do next thing\n"
        "  Blocked:\n"
        "    - Blocked thing (理由: waiting)"
    )

    tasks = load_task_state.parse_tasks(content)
    orders = load_task_state.parse_orders(content)

    assert tasks == expected_tasks
    assert load_task_state.format_summary(tasks, 20) == expected_summary
    assert orders == [{"name": "Legacy", "goal": None, "open_questions": 0}]
    assert load_task_state.format_summary(
        tasks, 20, orders=orders
    ) == load_task_state.format_summary(tasks, 20)


def test_archive_preserves_order_sections_for_completed_project(tmp_path) -> None:
    plans_path = tmp_path / "Plans.md"
    archive_path = tmp_path / "Plans.archive.md"
    content = """# Plans

## Project: Complete

#### Goal
- Preserve this goal

#### Context
- Preserve this context

### Phase 1: Done
#### Tasks
- `cc:done` Finished task

---

## Project: Active

### Phase 1: Work `cc:TODO`
#### Tasks
- `cc:TODO` Pending task
"""
    plans_path.write_text(content, encoding="utf-8")

    completed = load_task_state.detect_completed_projects(
        content,
        load_task_state.DEFAULT_MARKER_PATTERN,
        load_task_state.DEFAULT_MARKER_TO_STATE,
    )
    updated = load_task_state.archive_projects(plans_path, archive_path, completed, content)

    assert [project["name"] for project in completed] == ["Complete"]
    archive_text = archive_path.read_text(encoding="utf-8")
    assert "#### Goal\n- Preserve this goal" in archive_text
    assert "#### Context\n- Preserve this context" in archive_text
    assert "Preserve this goal" not in updated
    assert "Preserve this context" not in updated
    assert "## Project: Active" in updated


def test_frontmatter_is_inert_and_survives_archiving(tmp_path) -> None:
    frontmatter = """---
codd:
  node_id: "plan:test"
  kind: plan
  status: active
---
"""
    body = """# Plans

## Project: Test

#### Goal
- Ship safely

#### Open Questions
- None.

### Phase 1: Done
#### Tasks
- `cc:done` Finished task
"""
    plain_content = body
    frontmatter_content = f"{frontmatter}\n{body}"

    assert load_task_state.parse_tasks(frontmatter_content) == load_task_state.parse_tasks(
        plain_content
    )
    assert load_task_state.parse_orders(frontmatter_content) == load_task_state.parse_orders(
        plain_content
    )

    plain_completed = load_task_state.detect_completed_projects(
        plain_content,
        load_task_state.DEFAULT_MARKER_PATTERN,
        load_task_state.DEFAULT_MARKER_TO_STATE,
    )
    frontmatter_completed = load_task_state.detect_completed_projects(
        frontmatter_content,
        load_task_state.DEFAULT_MARKER_PATTERN,
        load_task_state.DEFAULT_MARKER_TO_STATE,
    )
    assert [project["name"] for project in frontmatter_completed] == [
        project["name"] for project in plain_completed
    ]
    assert [project["content"] for project in frontmatter_completed] == [
        project["content"] for project in plain_completed
    ]

    plans_path = tmp_path / "Plans.md"
    archive_path = tmp_path / "Plans.archive.md"
    plans_path.write_text(frontmatter_content, encoding="utf-8")
    updated = load_task_state.archive_projects(
        plans_path, archive_path, frontmatter_completed, frontmatter_content
    )
    plans_path.write_text(updated, encoding="utf-8")

    assert plans_path.read_text(encoding="utf-8").startswith(f"{frontmatter}\n# Plans")
