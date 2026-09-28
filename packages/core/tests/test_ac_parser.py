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
    """Issue #297 PR #326 review (High): `orchex init`/scaffold copies
    `templates/project/Plans.md` verbatim (`copy2`, no placeholder substitution) into every new
    project's `.claude/Plans.md`. If the template's Phase 1 example contained literal
    `#### Acceptance Criteria` / `- [ ]` placeholder lines, `/goal` and `/release-readiness`
    would read them as a real unchecked Acceptance Criteria, blocking Phase 1 from ever being
    considered complete. The template must therefore keep its
    guidance out of ac_parser's literal line-matching (e.g. behind an HTML comment whose lines
    never exactly equal the AC heading or start with a checkbox marker)."""

    def test_template_has_no_ac_heading_line(self) -> None:
        text = (REPO_ROOT / "templates" / "project" / "Plans.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        assert all(line.strip() != ac_parser.AC_SECTION_HEADING for line in lines)

    def test_template_has_no_unchecked_checkbox_line(self) -> None:
        text = (REPO_ROOT / "templates" / "project" / "Plans.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        assert all(ac_parser.classify_checkbox_line(line.strip()) != "unchecked" for line in lines)
