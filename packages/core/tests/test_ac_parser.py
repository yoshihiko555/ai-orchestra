"""ac_parser.py（AC 解析共有モジュール）の単体テスト。

Issue #299: load-task-state.py から切り出した AC 解析ロジックを、
モジュール単体で再利用できることを検証する。
"""

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


ac_parser = load_module("core_ac_parser", "packages/core/hooks/ac_parser.py")


class TestClassifyCheckboxLine:
    def test_unchecked_returns_unchecked(self) -> None:
        assert ac_parser.classify_checkbox_line("- [ ] condition") == "unchecked"

    def test_checked_lowercase_x_returns_checked(self) -> None:
        assert ac_parser.classify_checkbox_line("- [x] condition") == "checked"

    def test_checked_uppercase_x_returns_checked(self) -> None:
        assert ac_parser.classify_checkbox_line("- [X] condition") == "checked"

    def test_markdown_link_bullet_is_not_a_checkbox(self) -> None:
        # `- [text](url)` のような Markdown リンク箇条書きを誤検出しない
        assert ac_parser.classify_checkbox_line("- [text](url)") is None

    def test_plain_task_line_is_not_a_checkbox(self) -> None:
        assert ac_parser.classify_checkbox_line("- `cc:done` some task") is None

    def test_non_bullet_line_is_not_a_checkbox(self) -> None:
        assert ac_parser.classify_checkbox_line("plain text") is None


class TestProjectTemplateDoesNotFalsePositiveAsUncheckedAc:
    """未編集の雛形を実際の未チェック AC と誤認しない（Issue #297, PR #326）。"""

    def test_template_has_no_ac_heading_line(self) -> None:
        text = (REPO_ROOT / "templates" / "project" / "plan.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        assert all(line.strip() != ac_parser.AC_SECTION_HEADING for line in lines)

    def test_template_has_no_unchecked_checkbox_line(self) -> None:
        text = (REPO_ROOT / "templates" / "project" / "plan.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        assert all(ac_parser.classify_checkbox_line(line.strip()) != "unchecked" for line in lines)


class TestAcSections:
    def test_unchecked_ac_is_found_under_deeper_headings(self) -> None:
        lines = [
            "### Phase 1",
            "#### Acceptance Criteria",
            "- [x] done",
            "##### Details",
            "- [ ] nested requirement",
        ]
        assert ac_parser.has_unchecked_ac(lines, set()) is True
        assert ac_parser.ac_checkbox_line_indices(lines, set()) == {2, 4}

    def test_checked_or_excluded_ac_does_not_block_completion(self) -> None:
        lines = [
            "### Phase 1",
            "#### Acceptance Criteria",
            "- [x] done",
            "- [X] done",
            "- [ ] ignored",
        ]
        assert ac_parser.has_unchecked_ac(lines, {4}) is False
        assert ac_parser.ac_checkbox_line_indices(lines, {4}) == {2, 3}

    def test_ac_outside_phase_and_checkboxes_outside_ac_are_ignored(self) -> None:
        lines = [
            "#### Acceptance Criteria",
            "- [ ] outside phase",
            "### Phase 1",
            "- [ ] outside AC",
            "#### Acceptance Criteria",
            "- [x] passed",
            "#### Tasks",
            "- [ ] ordinary checklist",
        ]
        assert ac_parser.has_unchecked_ac(lines, set()) is False

    def test_ac_ends_at_each_heading_of_same_or_higher_level(self) -> None:
        for heading in ["# Notes", "## Notes", "### Phase 2", "#### Tasks"]:
            lines = [
                "### Phase 1",
                "#### Acceptance Criteria",
                "- [x] done",
                heading,
                "- [ ] outside",
            ]
            assert ac_parser.has_unchecked_ac(lines, set()) is False, heading

    def test_excluded_heading_does_not_start_ac(self) -> None:
        lines = ["### Phase 1", "#### Acceptance Criteria", "- [ ] outside AC"]
        assert ac_parser.has_unchecked_ac(lines, {1}) is False
