"""Tests for handoff.py — Codex CLI task handoff data collector."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

# Import the module under test
from facets.scripts.handoff import (
    attach_tasks_to_orders,
    collect_handoff_data,
    filter_sensitive_lines,
    find_project_root,
    parse_decisions,
    parse_order_sections,
    parse_tasks,
    render_order_markdown,
    structural_exclusions,
)

# ---------------------------------------------------------------------------
# parse_tasks
# ---------------------------------------------------------------------------


class TestParseTasks:
    def test_extracts_wip_tasks(self) -> None:
        content = "- `cc:WIP` Implement login form\n- `cc:WIP` Add tests"
        tasks = parse_tasks(content)
        assert len(tasks["WIP"]) == 2
        assert tasks["WIP"][0]["task"] == "Implement login form"

    def test_extracts_todo_tasks(self) -> None:
        content = "- `cc:TODO` Write documentation\n- `cc:TODO` Setup CI"
        tasks = parse_tasks(content)
        assert len(tasks["TODO"]) == 2

    def test_extracts_blocked_with_reason(self) -> None:
        content = "- `cc:blocked` Deploy to staging — 理由: waiting for approval"
        tasks = parse_tasks(content)
        assert len(tasks["blocked"]) == 1
        assert tasks["blocked"][0]["task"] == "Deploy to staging"
        assert tasks["blocked"][0]["reason"] == "waiting for approval"

    def test_ignores_done_tasks(self) -> None:
        content = "- `cc:done` Old task\n- `cc:WIP` Current task"
        tasks = parse_tasks(content)
        assert len(tasks["WIP"]) == 1
        assert "done" not in tasks

    def test_ignores_non_task_lines(self) -> None:
        content = "# Plans\n## Project: test\nSome text\n- `cc:WIP` Real task"
        tasks = parse_tasks(content)
        assert len(tasks["WIP"]) == 1

    def test_empty_content(self) -> None:
        tasks = parse_tasks("")
        assert tasks == {"WIP": [], "TODO": [], "blocked": []}

    def test_blocked_without_reason(self) -> None:
        content = "- `cc:blocked` Some blocked task"
        tasks = parse_tasks(content)
        assert len(tasks["blocked"]) == 1
        assert tasks["blocked"][0]["reason"] is None

    def test_skips_cc_marker_inside_order_context(self) -> None:
        content = (
            "## Project: Test\n"
            "#### Context\n"
            "- `cc:TODO` context note\n"
            "### Phase 1: Build `cc:TODO`\n"
            "#### Tasks\n"
            "- `cc:TODO` Real task\n"
        )

        tasks = parse_tasks(content)

        assert tasks["TODO"] == [
            {"task": "Real task", "reason": None, "project": "Test", "project_index": 0}
        ]

    def test_records_project_name_per_task_without_cross_project_leakage(self) -> None:
        content = (
            "## Project: Alpha\n"
            "### Phase 1: Build `cc:WIP`\n"
            "- `cc:WIP` Alpha task\n"
            "## Project: Beta\n"
            "### Phase 1: Build `cc:TODO`\n"
            "- `cc:TODO` Beta task\n"
        )

        tasks = parse_tasks(content)

        assert tasks["WIP"] == [
            {"task": "Alpha task", "reason": None, "project": "Alpha", "project_index": 0}
        ]
        assert tasks["TODO"] == [
            {"task": "Beta task", "reason": None, "project": "Beta", "project_index": 1}
        ]

    def test_nested_fence_requires_matching_char_and_length(self) -> None:
        content = (
            "````markdown\n```\n- `cc:WIP` fake nested task\n```\n````\n\n- `cc:WIP` real task\n"
        )

        tasks = parse_tasks(content)

        assert tasks["WIP"] == [
            {"task": "real task", "reason": None, "project": None, "project_index": None}
        ]

    def test_ignores_ac_checkbox_lines_even_with_cc_marker(self) -> None:
        content = (
            "## Project: Test\n"
            "### Phase 1: Build `cc:WIP`\n"
            "#### Acceptance Criteria\n"
            "- [ ] `cc:WIP` Condition met\n"
            "#### Tasks\n"
            "- `cc:WIP` Real task\n"
        )

        tasks = parse_tasks(content)

        assert tasks["WIP"] == [
            {"task": "Real task", "reason": None, "project": "Test", "project_index": 0}
        ]


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

    excluded = structural_exclusions(lines)

    assert {0, 1, 2, 3, 4, 5}.issubset(excluded)
    assert 6 not in excluded


# ---------------------------------------------------------------------------
# order sections
# ---------------------------------------------------------------------------


class TestParseOrderSections:
    def test_extracts_goal_context_and_constraints_bullets_verbatim(self) -> None:
        content = (
            "## Project: Test\n"
            "#### Goal\n"
            "- Ship v2\n"
            "- [ ] Keep checkbox text\n"
            "#### Context\n"
            "- ADR-001\n"
            "#### Out of Scope\n"
            "- Do not extract this\n"
            "#### Constraints\n"
            "- Python 3.12\n"
            "#### Open Questions\n"
            "- Do not extract this either\n"
            "### Phase 1: Build `cc:TODO`\n"
        )

        orders = parse_order_sections(content)

        assert orders == [
            {
                "name": "Test",
                "goal": ["Ship v2", "[ ] Keep checkbox text"],
                "context": ["ADR-001"],
                "constraints": ["Python 3.12"],
            }
        ]

    def test_project_without_order_sections_has_empty_project_order(self) -> None:
        content = "## Project: Legacy\n### Phase 1: Build `cc:TODO`\n"

        assert parse_order_sections(content) == [{"name": "Legacy"}]

    def test_empty_goal_section_omits_goal_key(self) -> None:
        content = (
            "## Project: Empty\n"
            "#### Goal\n"
            "\n"
            "#### Context\n"
            "- Existing context\n"
            "### Phase 1: Build `cc:TODO`\n"
        )

        assert parse_order_sections(content) == [{"name": "Empty", "context": ["Existing context"]}]

    def test_skips_placeholder_order_bullets(self) -> None:
        content = (
            "## Project: Scaffolded\n"
            "#### Goal\n"
            "- {目的。何のために、誰の何が変わるか}\n"
            "#### Context\n"
            "- {背景。なぜ今これをやるか}\n"
            "- Existing context\n"
            "### Phase 1: Build `cc:TODO`\n"
        )

        orders = parse_order_sections(content)

        assert orders == [{"name": "Scaffolded", "context": ["Existing context"]}]

    def test_ignores_leading_frontmatter(self) -> None:
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

        orders = parse_order_sections(content)

        assert orders == [{"name": "Real", "goal": ["Real goal"]}]

    def test_skips_html_comment_bullets(self) -> None:
        content = (
            "## Project: Test\n"
            "#### Context\n"
            "<!-- fill this in later -->\n"
            "- <!-- inline comment bullet -->\n"
            "- Real context note\n"
            "### Phase 1: Build `cc:TODO`\n"
        )

        orders = parse_order_sections(content)

        assert orders == [{"name": "Test", "context": ["Real context note"]}]

    def test_html_comment_heading_does_not_hijack_current_section(self) -> None:
        content = (
            "## Project: Test\n"
            "#### Context\n"
            "<!--\n"
            "#### Goal\n"
            "-->\n"
            "- Real context note\n"
            "### Phase 1: Build `cc:TODO`\n"
        )

        orders = parse_order_sections(content)

        assert orders == [{"name": "Test", "context": ["Real context note"]}]

    def test_fenced_code_block_inside_order_section_is_not_treated_as_structure(self) -> None:
        content = (
            "## Project: Test\n"
            "#### Context\n"
            "```\n"
            "### Phase 9: Fake `cc:WIP`\n"
            "#### Tasks\n"
            "- `cc:TODO` fake task\n"
            "```\n"
            "- Real context note\n"
            "### Phase 1: Build `cc:TODO`\n"
            "#### Tasks\n"
            "- `cc:TODO` Real task\n"
        )

        orders = parse_order_sections(content)
        tasks = parse_tasks(content)

        assert orders == [{"name": "Test", "context": ["Real context note"]}]
        assert tasks["TODO"] == [
            {"task": "Real task", "reason": None, "project": "Test", "project_index": 0}
        ]

    def test_preserves_duplicate_project_names_as_separate_entries(self) -> None:
        content = (
            "## Project: Dup\n"
            "#### Goal\n"
            "- First goal\n"
            "### Phase 1: Build `cc:TODO`\n"
            "## Project: Dup\n"
            "#### Goal\n"
            "- Second goal\n"
            "### Phase 1: Build `cc:TODO`\n"
        )

        orders = parse_order_sections(content)

        assert orders == [
            {"name": "Dup", "goal": ["First goal"]},
            {"name": "Dup", "goal": ["Second goal"]},
        ]

    def test_preserves_wrapped_bullet_continuation_lines(self) -> None:
        content = (
            "## Project: Test\n"
            "#### Context\n"
            "- Keep compatibility with all existing\n"
            "  plugins and config keys.\n"
            "- Second bullet\n"
            "### Phase 1: Build `cc:TODO`\n"
        )

        orders = parse_order_sections(content)

        assert orders == [
            {
                "name": "Test",
                "context": [
                    "Keep compatibility with all existing plugins and config keys.",
                    "Second bullet",
                ],
            }
        ]


class TestRenderOrderMarkdown:
    def test_attaches_tasks_to_matching_order_by_project_index(self) -> None:
        orders = [{"name": "Alpha"}, {"name": "Beta"}]
        tasks = {
            "WIP": [
                {
                    "task": "Alpha task",
                    "reason": None,
                    "project": "Alpha",
                    "project_index": 0,
                }
            ],
            "TODO": [],
            "blocked": [
                {
                    "task": "Legacy task",
                    "reason": None,
                    "project": None,
                    "project_index": None,
                }
            ],
        }

        attach_tasks_to_orders(orders, tasks)

        assert orders == [
            {
                "name": "Alpha",
                "tasks": {
                    "WIP": [{"task": "Alpha task", "reason": None}],
                    "TODO": [],
                    "blocked": [],
                },
            },
            {"name": "Beta"},
        ]

    def test_duplicate_project_sections_keep_tasks_separate(self) -> None:
        content = (
            "## Project: app\n"
            "### Phase 1: Build `cc:WIP`\n"
            "- `cc:WIP` First section task\n"
            "## Project: app\n"
            "### Phase 1: Build `cc:TODO`\n"
            "- `cc:TODO` Second section task\n"
        )

        orders = parse_order_sections(content)
        tasks = parse_tasks(content)
        attach_tasks_to_orders(orders, tasks)

        assert orders[0]["tasks"]["WIP"] == [{"task": "First section task", "reason": None}]
        assert orders[0]["tasks"]["TODO"] == []
        assert orders[1]["tasks"]["WIP"] == []
        assert orders[1]["tasks"]["TODO"] == [{"task": "Second section task", "reason": None}]

    def test_renders_present_sections_in_fixed_order(self) -> None:
        orders = [
            {
                "name": "Beta",
                "constraints": ["Python 3.12"],
                "goal": ["Ship v2"],
            },
            {"name": "Empty"},
        ]

        markdown = render_order_markdown(orders)

        assert markdown == (
            "## Order\n\n### Beta\n\n#### Goal\n\n- Ship v2\n\n#### Constraints\n\n- Python 3.12"
        )
        assert "#### Context" not in markdown
        assert "### Empty" not in markdown

    def test_returns_empty_string_for_empty_or_all_empty_orders(self) -> None:
        assert render_order_markdown([]) == ""
        assert render_order_markdown([{"name": "Empty"}]) == ""


# ---------------------------------------------------------------------------
# parse_decisions
# ---------------------------------------------------------------------------


class TestParseDecisions:
    def test_extracts_decisions(self) -> None:
        content = (
            "## Decisions\n\n"
            "- 2026-04-06: Use REST over GraphQL\n"
            "- 2026-04-05: Choose PostgreSQL\n\n"
            "## Notes\n\n- something"
        )
        decisions = parse_decisions(content)
        assert len(decisions) == 2
        assert "REST over GraphQL" in decisions[0]

    def test_ignores_template_placeholders(self) -> None:
        content = "## Decisions\n\n- {YYYY-MM-DD}: \n"
        decisions = parse_decisions(content)
        assert len(decisions) == 0

    def test_stops_at_next_section(self) -> None:
        content = "## Decisions\n\n- 2026-04-06: Decision 1\n\n## Notes\n\n- Not a decision"
        decisions = parse_decisions(content)
        assert len(decisions) == 1

    def test_no_decisions_section(self) -> None:
        content = "# Plans\n## Project: test\n- `cc:TODO` task"
        decisions = parse_decisions(content)
        assert decisions == []


# ---------------------------------------------------------------------------
# filter_sensitive_lines
# ---------------------------------------------------------------------------


class TestFilterSensitiveLines:
    def test_removes_env_files(self) -> None:
        stat = " .env          | 3 +++\n src/main.py   | 5 ++---"
        result = filter_sensitive_lines(stat)
        assert ".env" not in result
        assert "main.py" in result

    def test_removes_credential_files(self) -> None:
        stat = " credentials.json | 1 +\n app.py | 2 ++"
        result = filter_sensitive_lines(stat)
        assert "credentials" not in result
        assert "app.py" in result

    def test_empty_input(self) -> None:
        assert filter_sensitive_lines("") == ""

    def test_all_sensitive(self) -> None:
        stat = " .env | 1 +\n secret.key | 2 ++"
        result = filter_sensitive_lines(stat)
        assert result.strip() == ""


# ---------------------------------------------------------------------------
# find_project_root
# ---------------------------------------------------------------------------


class TestFindProjectRoot:
    def test_finds_root_with_claude_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        result = find_project_root(tmp_path)
        assert result == tmp_path

    def test_finds_root_in_parent(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        child = tmp_path / "src" / "lib"
        child.mkdir(parents=True)
        result = find_project_root(child)
        assert result == tmp_path

    def test_returns_cwd_when_no_claude_dir(self, tmp_path: Path) -> None:
        result = find_project_root(tmp_path)
        assert result == tmp_path


# ---------------------------------------------------------------------------
# collect_handoff_data
# ---------------------------------------------------------------------------


class TestCollectHandoffData:
    def test_error_when_no_plans(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        data = collect_handoff_data(tmp_path)
        assert "error" in data

    def test_collects_tasks_from_plans(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        plans = claude_dir / "Plans.md"
        plans.write_text(
            "# Plans\n\n"
            "## Project: Test\n\n"
            "### Phase 1: Setup `cc:WIP`\n\n"
            "- `cc:WIP` Task A\n"
            "- `cc:TODO` Task B\n\n"
            "## Decisions\n\n"
            "- 2026-04-06: Chose Python\n",
            encoding="utf-8",
        )

        with patch("facets.scripts.handoff.run_git", return_value=None):
            data = collect_handoff_data(tmp_path)

        assert "error" not in data
        assert len(data["tasks"]["WIP"]) == 1
        assert len(data["tasks"]["TODO"]) == 1
        assert data["tasks"]["WIP"][0]["task"] == "Task A"
        assert len(data["decisions"]) == 1
        assert data["order"] == [
            {
                "name": "Test",
                "tasks": {
                    "WIP": [{"task": "Task A", "reason": None}],
                    "TODO": [{"task": "Task B", "reason": None}],
                    "blocked": [],
                },
            }
        ]
        assert data["order_markdown"] == (
            "## Order\n\n### Test\n\n#### Tasks\n\n- `cc:WIP` Task A\n- `cc:TODO` Task B"
        )
        assert "timestamp" in data

    def test_collects_order_sections_and_markdown(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        plans = claude_dir / "Plans.md"
        plans.write_text(
            "# Plans\n\n"
            "## Project: Test\n\n"
            "#### Goal\n"
            "- Ship v2\n\n"
            "#### Context\n"
            "- ADR-001\n\n"
            "#### Constraints\n"
            "- Python 3.12\n\n"
            "### Phase 1: Setup `cc:WIP`\n\n"
            "#### Tasks\n"
            "- `cc:WIP` Task A\n",
            encoding="utf-8",
        )

        with patch("facets.scripts.handoff.run_git", return_value=None):
            data = collect_handoff_data(tmp_path)

        assert data["order"] == [
            {
                "name": "Test",
                "goal": ["Ship v2"],
                "context": ["ADR-001"],
                "constraints": ["Python 3.12"],
                "tasks": {
                    "WIP": [{"task": "Task A", "reason": None}],
                    "TODO": [],
                    "blocked": [],
                },
            }
        ]
        assert data["order_markdown"] == (
            "## Order\n\n"
            "### Test\n\n"
            "#### Goal\n\n"
            "- Ship v2\n\n"
            "#### Context\n\n"
            "- ADR-001\n\n"
            "#### Constraints\n\n"
            "- Python 3.12\n\n"
            "#### Tasks\n\n"
            "- `cc:WIP` Task A"
        )

    def test_associates_tasks_with_correct_project_only(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        plans = claude_dir / "Plans.md"
        plans.write_text(
            "# Plans\n\n"
            "## Project: Alpha\n\n"
            "#### Goal\n"
            "- Ship alpha\n\n"
            "### Phase 1: Build `cc:WIP`\n\n"
            "- `cc:WIP` Alpha task\n\n"
            "## Project: Beta\n\n"
            "### Phase 1: Build `cc:TODO`\n\n"
            "- `cc:TODO` Beta task\n",
            encoding="utf-8",
        )

        with patch("facets.scripts.handoff.run_git", return_value=None):
            data = collect_handoff_data(tmp_path)

        alpha = next(entry for entry in data["order"] if entry["name"] == "Alpha")
        beta = next(entry for entry in data["order"] if entry["name"] == "Beta")

        assert alpha["tasks"]["WIP"] == [{"task": "Alpha task", "reason": None}]
        assert alpha["tasks"]["TODO"] == []
        assert beta["tasks"]["TODO"] == [{"task": "Beta task", "reason": None}]
        assert beta["tasks"]["WIP"] == []
