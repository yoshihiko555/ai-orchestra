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


load_task_state = load_module("core_load_task_state", "packages/core/hooks/load-task-state.py")


def test_load_config_uses_project_override_without_ai_orchestra_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)

    config_dir = tmp_path / ".claude" / "config" / "core"
    config_dir.mkdir(parents=True)
    (config_dir / "task-memory.yaml").write_text(
        "show_summary_on_start: false\nmax_display_tasks: 7\n",
        encoding="utf-8",
    )

    config = load_task_state.load_config(str(tmp_path))

    assert config == {
        "show_summary_on_start": False,
        "max_display_tasks": 7,
    }


def test_load_config_returns_defaults_when_config_not_found(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)

    config = load_task_state.load_config(str(tmp_path))

    assert config == {
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
        "show_summary_on_start: true\nmax_display_tasks: 3\n",
        encoding="utf-8",
    )

    config = load_task_state.load_config(str(tmp_path))

    assert "plans_file" not in config
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


@pytest.mark.parametrize(
    ("limit", "shown"), [(0, 25), ("0", 25), ("invalid", 20), (-1, 20), (None, 20), (1, 1)]
)
def test_main_applies_display_limit(tmp_path, monkeypatch, capsys, limit, shown) -> None:
    plans_path = tmp_path / ".claude" / "plans" / "example.md"
    plans_path.parent.mkdir(parents=True)
    plans_path.write_text("\n".join(f"- `cc:TODO` task {i}" for i in range(25)), encoding="utf-8")
    monkeypatch.setattr(load_task_state, "read_hook_input", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(load_task_state, "load_config", lambda _: {"max_display_tasks": limit})

    load_task_state.main()

    assert capsys.readouterr().out.count("\n    - ") == shown


def test_main_prints_summary_when_only_orders_exist_and_no_tasks(tmp_path, monkeypatch) -> None:
    plans_path = tmp_path / ".claude" / "plans" / "example.md"
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
            "show_summary_on_start": True,
            "max_display_tasks": 20,
        },
    )
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda message: printed.append(message))

    load_task_state.main()

    assert printed
    assert "Goal: Ship it later" in printed[0]


def test_main_passes_custom_marker_mapping_to_parse_tasks(tmp_path, monkeypatch) -> None:
    plans_path = tmp_path / ".claude" / "plans" / "example.md"
    plans_path.parent.mkdir(parents=True)
    plans_path.write_text("- `x:todo` task", encoding="utf-8")

    monkeypatch.setattr(load_task_state, "read_hook_input", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(
        load_task_state,
        "load_config",
        lambda _project_dir: {
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


def test_main_preserves_completed_unfinished_and_legacy_files(
    tmp_path, monkeypatch, capsys
) -> None:
    """EV-50: 完了・未完了・旧ファイルの本文、Decisions / Notes を保持する。"""
    plans_dir = tmp_path / ".claude" / "plans"
    plans_dir.mkdir(parents=True)
    contents = {
        plans_dir / "completed.md": (
            "## Project: Completed\n#### Goal\n- Completed goal\n"
            "### Phase 1\n#### Acceptance Criteria\n- [x] passed\n"
            "#### Tasks\n- `cc:done` Finished task\n"
            "## Decisions\n- Completed decision\n## Notes\n- Completed note\n"
        ),
        plans_dir / "active.md": (
            "## Project: Active\n### Phase 1\n- `cc:TODO` Pending task\n"
            "## Decisions\n- Active decision\n## Notes\n- Active note\n"
        ),
        tmp_path / ".claude" / "Plans.md": "Legacy content\n",
    }
    for path, content in contents.items():
        path.write_text(content, encoding="utf-8")
    before = {path: path.read_bytes() for path in contents}
    monkeypatch.setattr(load_task_state, "read_hook_input", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(load_task_state, "load_config", lambda _: {})

    load_task_state.main()

    output = capsys.readouterr().out
    assert "未完了 1 / 完了 1" in output
    assert "Pending task" in output
    assert "/task-state close <slug>" in output
    assert "Completed goal" not in output
    assert "Finished task" not in output
    assert {path: path.read_bytes() for path in contents} == before
    assert set(plans_dir.iterdir()) == {path for path in contents if path.parent == plans_dir}
    assert not (tmp_path / ".claude" / "Plans.archive.md").exists()


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


def test_parse_orders_and_tasks_ignore_fenced_project_headings() -> None:
    """core EV-42: フェンス内の見出し（## Project: Fake 等）と cc: マーカーは、
    parse_orders / parse_tasks のいずれの構造解析にも影響しない。"""
    content = (
        "## Project: Real\n\n"
        "#### Context\n\n"
        "```\n"
        "## Project: Fake\n"
        "### Phase 1: Ghost `cc:done`\n"
        "- `cc:done` ghost task\n"
        "```\n\n"
        "### Phase 1: Actual `cc:WIP`\n\n"
        "- `cc:WIP` actual task\n"
    )

    orders = load_task_state.parse_orders(content)
    tasks = load_task_state.parse_tasks(content)

    assert orders == [{"name": "Real", "goal": None, "open_questions": 0}]
    assert tasks["done"] == []
    assert tasks["WIP"] == [{"task": "actual task", "reason": None}]


def test_parse_tasks_nested_fence_requires_matching_char_and_length() -> None:
    content = "````markdown\n```\n- `cc:WIP` fake nested task\n```\n````\n\n- `cc:WIP` real task\n"
    tasks = load_task_state.parse_tasks(content)
    assert tasks["WIP"] == [{"task": "real task", "reason": None}]


def test_structural_exclusions_frontmatter_closing_requires_unindented_delimiter() -> None:
    lines = [
        "---",
        "owner: |",
        "  team: infra",
        "  ---",
        "- `cc:WIP` bogus inside frontmatter",
        "---",
        "- `cc:WIP` real task",
    ]

    excluded = load_task_state.structural_exclusions(lines)

    assert {0, 1, 2, 3, 4, 5}.issubset(excluded)
    assert 6 not in excluded


def test_main_falls_back_to_default_markers_when_duplicates_exist(tmp_path, monkeypatch) -> None:
    plans_path = tmp_path / ".claude" / "plans" / "example.md"
    plans_path.parent.mkdir(parents=True)
    plans_path.write_text("- `dup:task` task", encoding="utf-8")

    monkeypatch.setattr(load_task_state, "read_hook_input", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(
        load_task_state,
        "load_config",
        lambda _project_dir: {
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


def test_parse_orders_open_questions_counts_only_top_level_bullets() -> None:
    content = (
        "## Project: Test\n\n"
        "#### Open Questions\n"
        "- Top level question one\n"
        "  - Nested sub-question\n"
        "- Top level question two\n"
    )
    orders = load_task_state.parse_orders(content)
    assert orders == [{"name": "Test", "goal": None, "open_questions": 2}]


def test_parse_orders_ignores_leading_frontmatter() -> None:
    content = (
        "---\n"
        "## Project: FrontmatterGhost\n"
        "#### Goal\n"
        "- Ghost goal\n"
        "---\n"
        "\n"
        "## Project: Real\n"
        "#### Goal\n"
        "- Real goal\n"
    )
    orders = load_task_state.parse_orders(content)
    assert orders == [{"name": "Real", "goal": "Real goal", "open_questions": 0}]


def test_parse_orders_goal_skips_html_comment_lines() -> None:
    content = (
        "## Project: Single\n\n"
        "#### Goal\n"
        "<!-- fill this in later -->\n"
        "- Actual goal text\n\n"
        "## Project: Multi\n\n"
        "#### Goal\n"
        "<!--\n"
        "multi\n"
        "line\n"
        "comment\n"
        "-->\n"
        "- Actual goal 2\n"
    )
    orders = load_task_state.parse_orders(content)
    assert orders == [
        {"name": "Single", "goal": "Actual goal text", "open_questions": 0},
        {"name": "Multi", "goal": "Actual goal 2", "open_questions": 0},
    ]


def test_html_comment_heading_does_not_hijack_current_section() -> None:
    content = (
        "## Project: Test\n"
        "#### Context\n"
        "<!--\n"
        "#### Goal\n"
        "-->\n"
        "- Real context note\n"
        "### Phase 1: Build `cc:TODO`\n"
    )

    orders = load_task_state.parse_orders(content)

    assert orders == [{"name": "Test", "goal": None, "open_questions": 0}]


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


def test_frontmatter_is_inert_for_task_and_order_parsing() -> None:
    """core EV-45: codd frontmatter は parse_tasks / parse_orders の構造解析に影響しない。"""
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


def _write_plan(project: Path, slug: str, content: str) -> Path:
    path = project / ".claude" / "plans" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("content", "complete"),
    [
        ("## Project: Empty\n#### Goal\n- Draft goal\n", False),
        ("", False),
        ("- `cc:done` Finished\n- `cc:WIP` Pending\n", False),
        ("- `cc:done` Finished\n- `cc:TODO` Pending\n", False),
        ("- `cc:done` Finished\n- `cc:blocked` Pending\n", False),
        (
            "### Phase 1\n#### Tasks\n- `cc:done` Finished\n"
            "#### Acceptance Criteria\n- [x] passed\n- [ ] missing\n",
            False,
        ),
        ("- `cc:done` Finished\n", True),
        (
            "### Phase 1\n#### Acceptance Criteria\n- [x] passed\n- [X] passed too\n"
            "#### Tasks\n- `cc:done` Finished\n",
            True,
        ),
        (
            "### Phase 1\n#### Acceptance Criteria\n- [X] passed\n"
            "##### Details\n- [ ] nested requirement\n#### Tasks\n- `cc:done` Finished\n",
            False,
        ),
        (
            "### Phase 1\n#### Acceptance Criteria\n- [X] passed\n"
            "#### Tasks\n- [ ] ordinary checklist\n- `cc:done` Finished\n",
            True,
        ),
        (
            "## Project: A\n### Phase 1\n- `cc:done` Finished\n"
            "## Project: B\n### Phase 1\n- `cc:TODO` Pending\n",
            False,
        ),
        (
            "## Project: A\n### Phase 1\n- `cc:done` First\n"
            "## Project: B\n### Phase 1\n- `cc:done` Second\n",
            True,
        ),
        (
            "#### Acceptance Criteria\n- [ ] outside phase\n### Phase 1\n- `cc:done` Finished\n",
            True,
        ),
    ],
)
def test_completion_requires_tasks_all_done_and_checked_ac(tmp_path, content, complete) -> None:
    path = _write_plan(tmp_path, "example", content)

    plans, invalid_names = load_task_state.collect_plans(str(tmp_path), {})
    summary = load_task_state.build_plans_summary(str(tmp_path), {})

    assert invalid_names == []
    assert len(plans) == 1
    assert plans[0]["path"] == path
    assert plans[0]["slug"] == "example"
    assert plans[0]["complete"] is complete
    assert ("■ example" in summary) is not complete


@pytest.mark.parametrize(
    "wrapper",
    [
        "```markdown\n{body}\n```\n",
        "~~~~markdown\n{body}\n~~~\n~~~~\n",
        "<!--\n{body}\n-->\n",
        "---\n{body}\n---\n",
    ],
)
def test_excluded_ac_headings_and_tasks_do_not_affect_completion(tmp_path, wrapper) -> None:
    excluded = wrapper.format(
        body="### Phase Fake\n#### Acceptance Criteria\n- [ ] fake\n- `cc:WIP` fake task"
    )
    _write_plan(tmp_path, "example", excluded + "### Phase 1\n- `cc:done` Finished\n")

    plans, _ = load_task_state.collect_plans(str(tmp_path), {})

    assert plans[0]["complete"] is True
    assert plans[0]["tasks"]["WIP"] == []


@pytest.mark.parametrize("wrapper", ["```\n{body}\n```\n", "<!--\n{body}\n-->\n"])
def test_excluded_heading_does_not_end_real_unchecked_ac(tmp_path, wrapper) -> None:
    _write_plan(
        tmp_path,
        "example",
        "### Phase 1\n#### Tasks\n- `cc:done` Finished\n#### Acceptance Criteria\n"
        + wrapper.format(body="#### Other section")
        + "- [ ] still required\n",
    )

    plans, _ = load_task_state.collect_plans(str(tmp_path), {})

    assert plans[0]["complete"] is False


@pytest.mark.parametrize("first_tasks", [2, 3])
def test_shared_cap_keeps_later_files_as_single_lines(tmp_path, first_tasks) -> None:
    _write_plan(
        tmp_path,
        "a-first",
        "## Project: First\n#### Goal\n- First goal\n#### Open Questions\n- First question\n"
        "### Phase 1\n" + "\n".join(f"- `cc:TODO` first task {i}" for i in range(first_tasks)),
    )
    _write_plan(tmp_path, "b-later", "- `cc:TODO` hidden second task\n")
    _write_plan(tmp_path, "c-later", "- `cc:blocked` hidden third task — 理由: waiting\n")

    summary = load_task_state.build_plans_summary(str(tmp_path), {"max_display_tasks": 2})

    assert summary.count("\n    - ") == 2
    assert "Goal: First goal" in summary
    assert "Open Questions: 1" in summary
    assert "■ b-later — WIP: 0, TODO: 1, blocked: 0 (表示上限のため省略)" in summary.splitlines()
    assert "■ c-later — WIP: 0, TODO: 0, blocked: 1 (表示上限のため省略)" in summary.splitlines()
    assert "hidden second task" not in summary
    assert "hidden third task" not in summary
    assert ("... and 1 more" in summary) is (first_tasks == 3)


def test_partial_file_keeps_goal_questions_and_all_omitted_counts(tmp_path) -> None:
    _write_plan(
        tmp_path, "a-idle", "## Project: Idle\n#### Goal\n- Hidden goal\n- `cc:TODO` excluded\n"
    )
    _write_plan(
        tmp_path,
        "z-active",
        "## Project: Active\n#### Goal\n- Active goal\n#### Open Questions\n- Pending question\n"
        "### Phase 1\n- `cc:TODO` next one\n- `cc:WIP` working\n- `cc:TODO` next two\n"
        "- `cc:blocked` waiting — 理由: external\n",
    )

    summary = load_task_state.build_plans_summary(str(tmp_path), {"max_display_tasks": 2})

    assert summary.index("■ z-active") < summary.index("■ a-idle")
    assert "Goal: Active goal" in summary
    assert "Open Questions: 1" in summary
    assert summary.index("    - working") < summary.index("    - next one")
    assert "... and 1 more" in summary
    assert "Blocked: (上限のため 1 件省略)" in summary
    assert "Hidden goal" not in summary
    assert "next two" not in summary


def test_wip_files_then_other_files_are_sorted_and_keep_their_own_orders(tmp_path) -> None:
    for slug, state in [
        ("z-idle", "TODO"),
        ("y-active", "WIP"),
        ("a-idle", "TODO"),
        ("b-active", "WIP"),
    ]:
        _write_plan(
            tmp_path,
            slug,
            f"## Project: {slug}\n#### Goal\n- {slug} goal\n"
            f"### Phase 1\n- `cc:{state}` {slug} task\n",
        )
    summary = load_task_state.build_plans_summary(str(tmp_path), {"max_display_tasks": 0})
    blocks = summary.split("■ ")[1:]

    assert [block.split(" ")[0] for block in blocks] == ["b-active", "y-active", "a-idle", "z-idle"]
    for slug, block in zip(["b-active", "y-active", "a-idle", "z-idle"], blocks, strict=True):
        assert f"Goal: {slug} goal" in block
        assert f"    - {slug} task" in block


@pytest.mark.parametrize("has_plans_dir", [False, True])
def test_legacy_warning_does_not_read_legacy_content(tmp_path, has_plans_dir) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    if has_plans_dir:
        (claude_dir / "plans").mkdir()
    (claude_dir / "Plans.md").write_bytes(b"\xff- `cc:WIP` legacy unique text")

    summary = load_task_state.build_plans_summary(str(tmp_path), {})

    assert summary == (
        "[task-memory] 警告: .claude/Plans.md は読み込まれません。"
        "Project ごとに .claude/plans/<slug>.md へ移してください"
    )
    assert "legacy unique text" not in summary


def test_broken_legacy_symlink_still_warns(tmp_path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "Plans.md").symlink_to(tmp_path / "missing.md")

    assert "警告" in load_task_state.build_plans_summary(str(tmp_path), {})


def test_summary_disabled_emits_neither_summary_nor_diagnostics(
    tmp_path, monkeypatch, capsys
) -> None:
    _write_plan(tmp_path, "example", "- `cc:TODO` Pending\n")
    _write_plan(tmp_path, "Invalid", "- `cc:TODO` Invalid task\n")
    (tmp_path / ".claude" / "Plans.md").write_text("legacy")
    monkeypatch.setattr(load_task_state, "read_hook_input", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(
        load_task_state,
        "load_config",
        lambda _: {"show_summary_on_start": False, "markers": {"todo": "dup", "wip": "dup"}},
    )

    load_task_state.main()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_plans_file_setting_is_ignored_and_local_markers_control_completion(tmp_path) -> None:
    _write_plan(tmp_path, "done", "- `local:done` Finished\n")
    _write_plan(tmp_path, "active", "- `local:todo` Pending\n")
    config_dir = tmp_path / ".claude" / "config" / "core"
    config_dir.mkdir(parents=True)
    (config_dir / "task-memory.yaml").write_text(
        "plans_file: .claude/custom.md\nmarkers:\n  todo: base:todo\n  done: base:done\n",
        encoding="utf-8",
    )
    (config_dir / "task-memory.local.yaml").write_text(
        "plans_file: [invalid, ignored]\nmarkers:\n  todo: local:todo\n  done: local:done\n",
        encoding="utf-8",
    )
    (tmp_path / ".claude" / "custom.md").write_text("- `local:todo` Never read this\n")
    config = load_task_state.load_config(str(tmp_path))

    summary = load_task_state.build_plans_summary(str(tmp_path), config)

    assert "未完了 1 / 完了 1" in summary
    assert "Pending" in summary
    assert "Never read this" not in summary
    assert "Finished" not in summary


@pytest.mark.parametrize("kind", ["missing", "empty", "file", "symlink"])
def test_invalid_or_empty_plans_dir_has_no_plans(tmp_path, kind) -> None:
    (tmp_path / ".claude").mkdir()
    plans_dir = tmp_path / ".claude" / "plans"
    if kind == "empty":
        plans_dir.mkdir()
    elif kind == "file":
        plans_dir.write_text("- `cc:TODO` not a directory\n")
    elif kind == "symlink":
        outside = tmp_path / "external"
        outside.mkdir()
        (outside / "external.md").write_text("- `cc:TODO` outside unique text\n")
        plans_dir.symlink_to(outside, target_is_directory=True)

    assert load_task_state.collect_plans(str(tmp_path), {}) == ([], [])
    assert load_task_state.build_plans_summary(str(tmp_path), {}) == ""


def test_discovery_ignores_symlinks_nested_dotfiles_and_invalid_markdown_names(tmp_path) -> None:
    _write_plan(tmp_path, "valid-1", "- `cc:TODO` visible task\n")
    plans_dir = tmp_path / ".claude" / "plans"
    for name in ["Foo.md", "a b.md", "x.MD", "-bad.md", "日本語.md", ".hidden.md", "notes.txt"]:
        (plans_dir / name).write_text("- `cc:WIP` hidden task\n")
    nested = plans_dir / "subdirectory.md"
    nested.mkdir()
    (nested / "nested.md").write_text("- `cc:WIP` nested task\n")
    outside = tmp_path / "outside.md"
    outside.write_text("- `cc:WIP` external unique text\n")
    (plans_dir / "external.md").symlink_to(outside)
    (plans_dir / "broken.md").symlink_to(tmp_path / "missing.md")

    plans, invalid_names = load_task_state.collect_plans(str(tmp_path), {})
    summary = load_task_state.build_plans_summary(str(tmp_path), {})

    assert [plan["slug"] for plan in plans] == ["valid-1"]
    assert invalid_names == ["-bad.md", "Foo.md", "a b.md", "x.MD", "日本語.md"]
    assert summary.splitlines()[-1] == (
        "[task-memory] 命名規則（<slug>.md, slug は [a-z0-9-]）に合わず読まなかったファイル: "
        "-bad.md, Foo.md, a b.md, x.MD, 日本語.md"
    )
    assert "visible task" in summary
    assert "hidden task" not in summary
    assert "nested task" not in summary
    assert "external unique text" not in summary


def test_non_utf8_plan_is_skipped_without_losing_other_files(tmp_path) -> None:
    _write_plan(tmp_path, "good", "- `cc:WIP` survives\n")
    bad = _write_plan(tmp_path, "bad", "")
    bad.write_bytes(b"\xff\xfe\x00")

    summary = load_task_state.build_plans_summary(str(tmp_path), {})

    assert "未完了 1 / 完了 0" in summary
    assert "survives" in summary
    assert "■ bad" not in summary


@pytest.mark.parametrize("failure", [PermissionError, FileNotFoundError])
def test_unreadable_or_disappeared_plan_does_not_hide_other_files(
    tmp_path, monkeypatch, failure
) -> None:
    _write_plan(tmp_path, "bad", "- `cc:WIP` unreadable\n")
    _write_plan(tmp_path, "good", "- `cc:TODO` survives\n")
    original_read = Path.read_text

    def read_text(path, *args, **kwargs):
        if path.name == "bad.md":
            raise failure("unreadable")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    summary = load_task_state.build_plans_summary(str(tmp_path), {})

    assert "survives" in summary
    assert "■ bad" not in summary


@pytest.mark.parametrize(
    "content",
    [
        "## Project: Draft\n#### Goal\n- Goal only\n",
        "- `cc:done` Finished\n",
        "- `cc:done` Finished\n- `cc:TODO` Pending\n",
        "### Phase 1\n#### Acceptance Criteria\n- [ ] missing\n#### Tasks\n- `cc:done` Finished\n",
        "### Phase 1\n#### Acceptance Criteria\n- [X] passed\n#### Tasks\n- `cc:done` Finished\n",
        "```\n#### Acceptance Criteria\n- [ ] fake\n```\n### Phase 1\n- `cc:done` Finished\n",
        "<!--\n#### Acceptance Criteria\n- [ ] fake\n-->\n### Phase 1\n- `cc:done` Finished\n",
        "---\n#### Acceptance Criteria\n- [ ] fake\n---\n### Phase 1\n- `cc:done` Finished\n",
    ],
)
def test_completion_agrees_with_handoff_for_shared_boundary_cases(tmp_path, content) -> None:
    """評価セット §5: 独立した 2 つのパーサで主要な完了境界を照合する。"""
    handoff = load_module("core_test_handoff", "facets/scripts/handoff.py")
    _write_plan(tmp_path, "example", content)

    plans, _ = load_task_state.collect_plans(str(tmp_path), {})
    handoff_tasks = handoff.parse_tasks(content, include_done=True)

    assert plans[0]["complete"] is handoff.plan_is_complete(content, handoff_tasks)
