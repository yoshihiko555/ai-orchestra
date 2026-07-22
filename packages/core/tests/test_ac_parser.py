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


class TestAcSectionRanges:
    def test_extracts_single_ac_section(self) -> None:
        lines = [
            "### Phase 1: Setup",
            "#### Acceptance Criteria",
            "- [ ] condition A",
            "- [x] condition B",
            "#### Tasks",
            "- `cc:done` task",
        ]
        ranges = ac_parser.ac_section_ranges(lines, phase_start=0, phase_end=5)
        assert ranges == [(2, 3)]

    def test_ac_section_extends_to_phase_end_when_no_next_heading(self) -> None:
        lines = [
            "### Phase 1: Setup",
            "#### Acceptance Criteria",
            "- [ ] condition A",
            "- [x] condition B",
        ]
        ranges = ac_parser.ac_section_ranges(lines, phase_start=0, phase_end=3)
        assert ranges == [(2, 3)]

    def test_no_ac_section_returns_empty_list(self) -> None:
        lines = [
            "### Phase 1: Setup",
            "#### Tasks",
            "- `cc:done` task",
        ]
        ranges = ac_parser.ac_section_ranges(lines, phase_start=0, phase_end=2)
        assert ranges == []

    def test_empty_ac_section_returns_empty_range(self) -> None:
        lines = [
            "### Phase 1: Setup",
            "#### Acceptance Criteria",
            "#### Tasks",
            "- `cc:done` task",
        ]
        ranges = ac_parser.ac_section_ranges(lines, phase_start=0, phase_end=3)
        assert ranges == [(2, 1)]


class TestPhaseHasUncheckedAc:
    def test_returns_true_when_unchecked_line_present(self) -> None:
        lines = ["- [ ] condition A", "- [x] condition B"]
        assert ac_parser.phase_has_unchecked_ac(lines, [(0, 1)]) is True

    def test_returns_false_when_all_checked(self) -> None:
        lines = ["- [x] condition A", "- [X] condition B"]
        assert ac_parser.phase_has_unchecked_ac(lines, [(0, 1)]) is False

    def test_returns_false_for_empty_ranges(self) -> None:
        lines = ["- [ ] condition A"]
        assert ac_parser.phase_has_unchecked_ac(lines, []) is False
