"""Tests for handoff.py — Codex CLI task handoff data collector."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the module under test
from facets.scripts.handoff import (
    CODEX_MODEL_SAFE_PATTERN,
    INTEGRATION_BRANCHES,
    attach_tasks_to_orders,
    build_launch,
    collect_handoff_data,
    filter_sensitive_lines,
    find_project_root,
    get_branch_status,
    get_untracked_files,
    handoff_file_path,
    load_cli_tools,
    main,
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

    def test_includes_done_tasks_for_completion_without_changing_default_shape(self) -> None:
        content = "- `cc:done` Old task\n- `cc:WIP` Current task"

        tasks = parse_tasks(content, include_done=True)

        assert tasks["done"] == [
            {"task": "Old task", "reason": None, "project": None, "project_index": None}
        ]
        assert set(tasks) == {"WIP", "TODO", "blocked", "done"}

    def test_ignores_non_task_lines(self) -> None:
        content = "## Project: test\nSome text\n- `cc:WIP` Real task"
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
        content = "## Project: test\n- `cc:TODO` task"
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
    def test_no_plan_directory_returns_empty_selection(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        data = collect_handoff_data(tmp_path)
        assert data["plan_file"] is None
        assert data["plan_candidates"] == []
        assert data["tasks"] == {"WIP": [], "TODO": [], "blocked": []}
        assert data["order"] == []
        assert data["decisions"] == []
        assert data["order_markdown"] == ""
        assert data["plans_content"] == ""

    def test_collects_tasks_from_plans(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude" / "plans"
        claude_dir.mkdir(parents=True)
        plans = claude_dir / "fixture.md"
        plans.write_text(
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
        claude_dir = tmp_path / ".claude" / "plans"
        claude_dir.mkdir(parents=True)
        plans = claude_dir / "fixture.md"
        plans.write_text(
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
        claude_dir = tmp_path / ".claude" / "plans"
        claude_dir.mkdir(parents=True)
        plans = claude_dir / "fixture.md"
        plans.write_text(
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


# ---------------------------------------------------------------------------
# launch command / branch status / untracked files (EV-48 / EV-49)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_CODEX = {
    "enabled": True,
    "model": "gpt-5.6-sol",
    "sandbox": {"analysis": "read-only", "implementation": "workspace-write"},
    "flags": "--search",
}


def _config(**codex_overrides: object) -> dict:
    return {"codex": {**VALID_CODEX, **codex_overrides}}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("x\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


class TestBuildLaunch:
    def test_command_passes_each_value_as_one_argument(self, tmp_path: Path) -> None:
        project = tmp_path / "my proj's dir"
        handoff = project / ".claude" / "handoffs" / "20260927-000000.md"
        handoff.parent.mkdir(parents=True)
        # Shell metacharacters in the file must reach codex verbatim, not be evaluated.
        content = "Task: fix it\n$(printf injected) `printf injected` 'single' \"double\""
        handoff.write_text(content, encoding="utf-8")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "codex"
        fake.write_text(
            "#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)

        launch = build_launch(project, handoff, _config())

        assert launch["available"] is True
        assert launch["reason"] is None
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        result = subprocess.run(
            ["bash", "-c", str(launch["command"])],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout) == [
            "-C",
            str(project),
            "--model",
            "gpt-5.6-sol",
            "--sandbox",
            "workspace-write",
            content,
        ]

    def test_flags_are_not_part_of_the_launch_command(self, tmp_path: Path) -> None:
        launch = build_launch(tmp_path, tmp_path / "h.md", _config(flags="--search"))
        assert "--search" not in str(launch["command"])

    @pytest.mark.parametrize(
        ("config", "reason_part"),
        [
            (None, "could not be loaded"),
            ({}, "codex.enabled"),
            ({"codex": "broken"}, "codex.enabled"),
            (_config(enabled=False), "codex.enabled"),
            (_config(enabled="true"), "codex.enabled"),
            ({"codex": {k: v for k, v in VALID_CODEX.items() if k != "enabled"}}, "codex.enabled"),
            (_config(sandbox={"implementation": "read-only"}), "sandbox"),
            (_config(sandbox={"implementation": "danger-full-access"}), "sandbox"),
            (_config(sandbox="workspace-write"), "sandbox"),
            (_config(model="gpt;touch x"), "codex.model"),
            (_config(model="gpt 5"), "codex.model"),
            (_config(model="gpt\n5"), "codex.model"),
            (_config(model="$(printf x)"), "codex.model"),
            (_config(model=""), "codex.model"),
            (_config(model=None), "codex.model"),
            (_config(model=5), "codex.model"),
        ],
    )
    def test_unsafe_or_disabled_config_produces_no_command(
        self, tmp_path: Path, config: dict | None, reason_part: str
    ) -> None:
        launch = build_launch(tmp_path, tmp_path / "h.md", config)
        assert launch["available"] is False
        assert launch["command"] is None
        assert reason_part in str(launch["reason"])

    def test_model_pattern_matches_routing_hook(self) -> None:
        hooks_dir = str(REPO_ROOT / "packages" / "core" / "hooks")
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import hook_common

        assert CODEX_MODEL_SAFE_PATTERN.pattern == hook_common._CODEX_MODEL_SAFE_PATTERN.pattern


class TestLoadCliTools:
    def test_returns_none_without_orchestra_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
        assert load_cli_tools(tmp_path) is None

    def test_local_override_is_applied_and_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
        config_dir = tmp_path / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True)
        (config_dir / "cli-tools.yaml").write_text(
            "codex:\n  enabled: true\n  model: gpt-5.6-sol\n"
            "  sandbox:\n    analysis: read-only\n    implementation: workspace-write\n",
            encoding="utf-8",
        )
        (config_dir / "cli-tools.local.yaml").write_text(
            "codex:\n  sandbox:\n    implementation: read-only\n", encoding="utf-8"
        )

        config = load_cli_tools(tmp_path)

        assert config is not None
        assert config["codex"]["model"] == "gpt-5.6-sol"
        launch = build_launch(tmp_path, tmp_path / "h.md", config)
        assert launch["available"] is False
        assert "sandbox" in str(launch["reason"])

    @pytest.mark.parametrize("local_text", ["codex: [unclosed\n", "- just\n- a list\n"])
    def test_unreadable_local_override_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_text: str
    ) -> None:
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
        config_dir = tmp_path / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True)
        (config_dir / "cli-tools.local.yaml").write_text(local_text, encoding="utf-8")

        assert load_cli_tools(tmp_path) is None


class TestBranchStatus:
    def test_integration_branch_list_matches_base_branch_resolver(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "resolve_base_branch",
            REPO_ROOT / "packages" / "git-workflow" / "scripts" / "resolve_base_branch.py",
        )
        assert spec is not None and spec.loader is not None
        resolver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resolver)
        assert set(INTEGRATION_BRANCHES) == set(resolver.CANDIDATES)

    def test_main_is_integration_and_feature_branch_is_not(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        assert get_branch_status(repo) == {
            "git_repository": True,
            "current": "main",
            "detached": False,
            "default_branch": None,
            "on_integration_branch": True,
        }
        _git(repo, "switch", "-q", "-c", "feat/x")
        assert get_branch_status(repo)["on_integration_branch"] is False

    def test_remote_default_branch_counts_as_integration(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        _git(repo, "update-ref", "refs/remotes/origin/trunk", "HEAD")
        _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        _git(repo, "switch", "-q", "-c", "trunk")

        status = get_branch_status(repo)

        assert status["default_branch"] == "trunk"
        assert status["on_integration_branch"] is True

    def test_default_branch_from_a_non_origin_remote_ref(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        _git(repo, "update-ref", "refs/remotes/upstream/trunk", "HEAD")
        _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/upstream/trunk")

        assert get_branch_status(repo)["default_branch"] == "trunk"

    def test_not_a_repository_is_not_reported_as_detached(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()

        assert get_branch_status(plain) == {
            "git_repository": False,
            "current": None,
            "detached": False,
            "default_branch": None,
            "on_integration_branch": False,
        }

    def test_detached_head_is_treated_as_integration(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        _git(repo, "switch", "-q", "--detach", "HEAD")

        status = get_branch_status(repo)

        assert status["current"] is None
        assert status["detached"] is True
        assert status["on_integration_branch"] is True


class TestUntrackedFiles:
    def test_lists_untracked_excluding_ignored_and_sensitive(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        (repo / "ignored.txt").write_text("x", encoding="utf-8")
        (repo / "a.py").write_text("x", encoding="utf-8")
        (repo / ".env").write_text("TOKEN=x", encoding="utf-8")
        (repo / "sub").mkdir()
        (repo / "sub" / "b file.md").write_text("x", encoding="utf-8")
        (repo / "sub" / "secret.txt").write_text("x", encoding="utf-8")

        files, truncated = get_untracked_files(repo)

        assert sorted(files) == [".gitignore", "a.py", "sub/b file.md"]
        assert truncated is False

    def test_carriage_return_in_file_name_is_kept(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        (repo / "weird\rname.txt").write_text("x", encoding="utf-8")

        files, _ = get_untracked_files(repo)

        assert files == ["weird\rname.txt"]

    def test_truncates_over_limit(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        for name in ("a.py", "b.py", "c.py"):
            (repo / name).write_text("x", encoding="utf-8")

        files, truncated = get_untracked_files(repo, limit=2)

        assert len(files) == 2
        assert truncated is True


class TestCollectHandoffLaunchFields:
    def test_handoff_path_and_launch_use_the_same_absolute_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
        (tmp_path / ".claude" / "plans").mkdir(parents=True)
        (tmp_path / ".claude" / "plans" / "fixture.md").write_text(
            "## Project: Test\n", encoding="utf-8"
        )

        with patch("facets.scripts.handoff.run_git", return_value=None):
            data = collect_handoff_data(tmp_path)

        handoff_path = Path(data["handoff_path"])
        assert handoff_path.is_absolute()
        assert handoff_path.parent == tmp_path.resolve() / ".claude" / "handoffs"
        assert set(data["branch_status"]) == {
            "git_repository",
            "current",
            "detached",
            "default_branch",
            "on_integration_branch",
        }
        assert data["untracked_files"] == []
        assert data["untracked_truncated"] is False

    def test_launch_command_reads_the_reported_handoff_path(self, tmp_path: Path) -> None:
        (tmp_path / ".claude" / "plans").mkdir(parents=True)
        (tmp_path / ".claude" / "plans" / "fixture.md").write_text(
            "## Project: Test\n", encoding="utf-8"
        )

        with (
            patch("facets.scripts.handoff.run_git", return_value=None),
            patch("facets.scripts.handoff.load_cli_tools", return_value=_config()),
        ):
            data = collect_handoff_data(tmp_path)

        assert data["launch"]["available"] is True
        assert data["launch"]["command"].endswith(f'"$(cat {shlex.quote(data["handoff_path"])})"')

    def test_existing_handoff_file_is_not_reused(self, tmp_path: Path) -> None:
        now = datetime(2026, 9, 27, 1, 2, 3, tzinfo=UTC)
        handoffs = tmp_path / ".claude" / "handoffs"
        handoffs.mkdir(parents=True)
        (handoffs / "20260927-010203.md").write_text("first", encoding="utf-8")

        path = handoff_file_path(tmp_path, now)

        assert path == tmp_path.resolve() / ".claude" / "handoffs" / "20260927-010203-2.md"


def _write_plan(project: Path, slug: str, content: str) -> Path:
    path = project / ".claude" / "plans" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestPlanSelection:
    @pytest.fixture(autouse=True)
    def _isolate_context(self):
        with (
            patch("facets.scripts.handoff.run_git", return_value=None),
            patch("facets.scripts.handoff.get_untracked_files", return_value=([], False)),
            patch("facets.scripts.handoff.load_cli_tools", return_value=None),
            patch("facets.scripts.handoff.load_working_context", return_value={}),
        ):
            yield

    @pytest.mark.parametrize("slug", ["../x", "Foo", "a b", "", "a/b", "a\n", "-a"])
    def test_invalid_slug_returns_error_without_reading_files(self, tmp_path: Path, slug: str):
        with (
            patch.object(Path, "read_text", side_effect=AssertionError("ファイル読込は禁止")),
            patch.object(Path, "read_bytes", side_effect=AssertionError("ファイル読込は禁止")),
            patch.object(Path, "iterdir", side_effect=AssertionError("列挙は禁止")),
        ):
            assert "error" in collect_handoff_data(tmp_path, plan=slug)

    @pytest.mark.parametrize("slug", ["../x", "Foo", "a b"])
    def test_cli_rejects_invalid_slug_before_project_discovery(self, slug: str, capsys):
        with (
            patch.object(sys, "argv", ["handoff.py", "--plan", slug]),
            patch("facets.scripts.handoff.find_project_root", side_effect=AssertionError),
            patch.object(Path, "read_text", side_effect=AssertionError),
        ):
            main()
        assert "error" in json.loads(capsys.readouterr().out)

    def test_cli_selects_requested_plan(self, tmp_path: Path, capsys):
        _write_plan(tmp_path, "fixture", "- `cc:TODO` Selected task\n")
        with patch.object(
            sys, "argv", ["handoff.py", "--project-dir", str(tmp_path), "--plan", "fixture"]
        ):
            main()
        assert json.loads(capsys.readouterr().out)["plan_file"] == ".claude/plans/fixture.md"

    def test_missing_requested_file_returns_error(self, tmp_path: Path):
        _write_plan(tmp_path, "other", "- `cc:TODO` Other task\n")
        assert "error" in collect_handoff_data(tmp_path, plan="missing")

    def test_requested_plan_with_missing_directory_returns_error(self, tmp_path: Path):
        assert "error" in collect_handoff_data(tmp_path, plan="missing")

    @pytest.mark.parametrize("kind", ["symlink", "broken-symlink", "directory"])
    def test_non_regular_plan_is_never_read(self, tmp_path: Path, kind: str):
        plans = tmp_path / ".claude" / "plans"
        plans.mkdir(parents=True)
        path = plans / "fixture.md"
        if kind == "directory":
            path.mkdir()
        else:
            target = tmp_path / "outside.md"
            if kind == "symlink":
                target.write_text("- `cc:WIP` Outside task\n", encoding="utf-8")
            path.symlink_to(target)
        with patch.object(Path, "read_text", side_effect=AssertionError):
            data = collect_handoff_data(tmp_path)
            assert data["plan_file"] is None
            assert data["plan_candidates"] == []
            assert "error" in collect_handoff_data(tmp_path, plan="fixture")

    @pytest.mark.parametrize("kind", ["symlink", "file"])
    def test_non_directory_plans_path_is_never_read(self, tmp_path: Path, kind: str):
        plans = tmp_path / ".claude" / "plans"
        plans.parent.mkdir()
        if kind == "symlink":
            outside = tmp_path / "outside"
            outside.mkdir()
            (outside / "fixture.md").write_text("- `cc:WIP` Outside task\n", encoding="utf-8")
            plans.symlink_to(outside, target_is_directory=True)
        else:
            plans.write_text("- `cc:WIP` Outside task\n", encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=AssertionError):
            assert collect_handoff_data(tmp_path)["plan_candidates"] == []
            assert "error" in collect_handoff_data(tmp_path, plan="fixture")

    def test_multiple_unfinished_plans_return_ordered_candidates_and_empty_content(self, tmp_path):
        _write_plan(tmp_path, "a-todo", "- `cc:TODO` First TODO\n")
        _write_plan(tmp_path, "z-wip", "- `cc:WIP` Last WIP\n")
        _write_plan(tmp_path, "b-wip", "- `cc:WIP` First WIP\n")
        _write_plan(tmp_path, "b-blocked", "- `cc:blocked` Waiting\n")
        _write_plan(tmp_path, "complete", "- `cc:done` Finished\n")

        data = collect_handoff_data(tmp_path)

        assert data["plan_file"] is None
        assert data["plan_candidates"] == ["b-wip", "z-wip", "a-todo", "b-blocked"]
        assert data["tasks"] == {"WIP": [], "TODO": [], "blocked": []}
        assert data["order"] == []
        assert data["decisions"] == []
        assert data["order_markdown"] == ""
        assert data["plans_content"] == ""

    def test_only_unfinished_plan_is_selected_and_other_content_does_not_leak(self, tmp_path):
        content = (
            "## Project: Current\n#### Goal\n- Current goal\n"
            "### Phase 1: Build\n#### Tasks\n- `cc:TODO` Current task\n"
            "## Decisions\n- Current decision\n"
        )
        _write_plan(tmp_path, "current", content)
        _write_plan(
            tmp_path,
            "complete",
            content.replace("Current", "Completed").replace("cc:TODO", "cc:done"),
        )

        data = collect_handoff_data(tmp_path)

        assert data["plan_file"] == ".claude/plans/current.md"
        assert data["plan_candidates"] == []
        assert data["plans_content"] == content
        assert data["decisions"] == ["Current decision"]
        assert "Current task" in data["order_markdown"]
        assert "Completed" not in json.dumps(data["order"])
        assert set(data["tasks"]) == {"WIP", "TODO", "blocked"}

    def test_explicit_plan_selects_completed_file_among_other_candidates(self, tmp_path):
        content = "## Project: Complete\n### Phase 1: Build\n- `cc:done` Finished\n"
        _write_plan(tmp_path, "complete", content)
        _write_plan(tmp_path, "current", "- `cc:WIP` Current task\n")

        data = collect_handoff_data(tmp_path, plan="complete")

        assert data["plan_file"] == ".claude/plans/complete.md"
        assert data["plan_candidates"] == []
        assert data["plans_content"] == content
        assert data["tasks"] == {"WIP": [], "TODO": [], "blocked": []}

    @pytest.mark.parametrize("filename", ["Foo.md", "a b.md", "a.MD", ".hidden.md", "a.txt"])
    def test_invalid_filename_is_not_read(self, tmp_path, filename):
        valid = _write_plan(tmp_path, "valid", "- `cc:TODO` Real task\n")
        (valid.parent / filename).write_bytes(b"\xff")
        nested = valid.parent / "nested"
        nested.mkdir()
        (nested / "other.md").write_text("- `cc:WIP` Nested task\n", encoding="utf-8")
        assert collect_handoff_data(tmp_path)["plan_file"] == ".claude/plans/valid.md"

    @pytest.mark.parametrize("failure", ["utf8", "oserror"])
    def test_unreadable_file_is_skipped_but_explicit_selection_returns_error(
        self, tmp_path, monkeypatch, failure
    ):
        bad = _write_plan(tmp_path, "bad", "- `cc:WIP` Unreadable task\n")
        _write_plan(tmp_path, "good", "- `cc:TODO` Readable task\n")
        if failure == "utf8":
            bad.write_bytes(b"\xff")
        else:
            original_read = Path.read_text

            def read_plan(path, *args, **kwargs):
                if path == bad:
                    raise OSError("読込失敗")
                return original_read(path, *args, **kwargs)

            monkeypatch.setattr(Path, "read_text", read_plan)

        assert collect_handoff_data(tmp_path)["plan_file"] == ".claude/plans/good.md"
        assert "error" in collect_handoff_data(tmp_path, plan="bad")

    def test_legacy_plan_is_never_read(self, tmp_path):
        legacy = tmp_path / ".claude" / "Plans.md"
        legacy.parent.mkdir()
        legacy.write_text("- `cc:WIP` Legacy task\n", encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=AssertionError):
            data = collect_handoff_data(tmp_path)
        assert data["plan_file"] is None
        assert data["plan_candidates"] == []
        assert data["tasks"] == {"WIP": [], "TODO": [], "blocked": []}


_COMPLETION_CASES = [
    pytest.param("## Project: Empty\n", False, id="zero-tasks"),
    pytest.param("- `cc:done` Finished\n", True, id="all-done-without-ac"),
    pytest.param("- `cc:done` Finished\n- `cc:WIP` Working\n", False, id="wip-remains"),
    pytest.param("- `cc:done` Finished\n- `cc:TODO` Pending\n", False, id="todo-remains"),
    pytest.param("- `cc:done` Finished\n- `cc:blocked` Waiting\n", False, id="blocked-remains"),
    pytest.param(
        "#### Acceptance Criteria\n- [ ] Pending\n#### Tasks\n- `cc:done` Finished\n",
        False,
        id="unchecked-ac",
    ),
    pytest.param(
        "#### Acceptance Criteria\n- [x] First\n- [X] Second\n#### Tasks\n- `cc:done` Finished\n",
        True,
        id="checked-ac-both-cases",
    ),
    pytest.param(
        "#### Acceptance Criteria\n- [x] Passed\n##### Nested\n- [ ] Pending\n"
        "#### Tasks\n- `cc:done` Finished\n",
        False,
        id="nested-ac-heading-does-not-end-section",
    ),
    *[
        pytest.param(
            f"#### Acceptance Criteria\n- [x] Passed\n{heading} Other\n- [ ] Not AC\n"
            "- `cc:done` Finished\n",
            True,
            id=f"ac-ends-at-level-{len(heading)}",
        )
        for heading in ("#", "##", "###", "####")
    ],
    pytest.param(
        "#### Acceptance Criteria\n```markdown\n- [ ] Fenced AC\n"
        "- `cc:TODO` Fenced task\n```\n- [X] Passed\n#### Tasks\n- `cc:done` Finished\n",
        True,
        id="fenced-ac-and-task-ignored",
    ),
    *[
        pytest.param(
            f"#### Tasks\n- `cc:done` Finished\n{opening}\n"
            f"#### Acceptance Criteria\n- [ ] Hidden\n- `cc:TODO` Hidden task\n{closing}",
            False,
            id=f"unclosed-fence-{boundary}",
        )
        for opening, closing, boundary in (
            ("```", "", "at-eof"),
            ("~~~", "", "tilde-at-eof"),
            ("````", "```\n", "shorter-closing"),
            ("```", "~~~\n", "different-closing-char"),
            ("```", "``` trailing\n", "closing-not-alone"),
        )
    ],
    pytest.param(
        "---\nexample: |\n  ```\n---\n### Phase 1\n- `cc:done` Finished\n",
        True,
        id="fence-in-frontmatter-ignored",
    ),
    pytest.param(
        "---\n#### Acceptance Criteria\n- [ ] Frontmatter AC\n- `cc:WIP` Fake\n---\n"
        "## Project: Fixture\n### Phase 1: Build\n"
        "#### Acceptance Criteria\n<!--\n- [ ] Comment AC\n- `cc:WIP` Fake\n-->\n"
        "- [x] Passed\n#### Tasks\n- `cc:done` Finished\n",
        True,
        id="frontmatter-and-comments-ignored",
    ),
    pytest.param(
        "#### Acceptance Criteria\n- [X] `cc:TODO` AC is not a task\n"
        "#### Tasks\n- `cc:done` Finished\n",
        True,
        id="marker-in-ac-is-not-task",
    ),
    pytest.param(
        "## Project: First\n### Phase 1\n- `cc:done` Finished\n"
        "## Project: Second\n### Phase 2\n#### Acceptance Criteria\n- [ ] Pending\n"
        "#### Tasks\n- `cc:done` Also finished\n",
        False,
        id="all-projects-contribute-to-completion",
    ),
    pytest.param(
        "---\ncodd: {}\n---\n## Project: Fixture\n#### Acceptance Criteria\n"
        "- [ ] Outside any Phase\n### Phase 1: Build\n#### Tasks\n- `cc:done` Finished\n",
        True,
        id="ac-outside-phase-is-not-ac",
    ),
]


@pytest.mark.parametrize(("body", "complete"), _COMPLETION_CASES)
def test_completion_boundaries_match_session_start(tmp_path: Path, body: str, complete: bool):
    content = (
        body if body.startswith("---\n") else "## Project: Fixture\n### Phase 1: Build\n" + body
    )
    _write_plan(tmp_path, "fixture", content)
    with (
        patch("facets.scripts.handoff.run_git", return_value=None),
        patch("facets.scripts.handoff.get_untracked_files", return_value=([], False)),
        patch("facets.scripts.handoff.load_cli_tools", return_value=None),
    ):
        data = collect_handoff_data(tmp_path)

    assert data["plan_file"] == (None if complete else ".claude/plans/fixture.md")
    assert data["plan_candidates"] == []
    assert data["plans_content"] == ("" if complete else content)

    hooks = str(REPO_ROOT / "packages" / "core" / "hooks")
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    spec = importlib.util.spec_from_file_location(
        "handoff_completion_hook", Path(hooks) / "load-task-state.py"
    )
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    plans, _ = hook.collect_plans(str(tmp_path), config={})
    assert len(plans) == 1
    assert plans[0]["complete"] is complete
