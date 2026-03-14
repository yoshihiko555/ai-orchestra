# ruff: noqa: I001
import sys
from pathlib import Path
import importlib
import re
from io import StringIO

sys.path.insert(0, str(Path(__file__).parent.parent / "packages" / "core" / "hooks"))
_mod = importlib.import_module("load-task-state")
detect_completed_projects = _mod.detect_completed_projects
archive_projects = _mod.archive_projects
DEFAULT_MARKER_PATTERN = _mod.DEFAULT_MARKER_PATTERN
DEFAULT_MARKER_TO_STATE = _mod.DEFAULT_MARKER_TO_STATE
DEFAULT_MARKERS = _mod.DEFAULT_MARKERS


def test_detect_completed_projects_all_done() -> None:
    content = (
        "# Plans\n"
        "\n"
        "## Project: Alpha\n"
        "### Phase 1: Design `cc:done`\n"
        "### Phase 2: Build\n"
        "- `cc:done` implement feature\n"
        "---\n"
        "## Decisions\n"
        "- 2026-03-14: keep format\n"
    )

    completed = detect_completed_projects(content, DEFAULT_MARKER_PATTERN, DEFAULT_MARKER_TO_STATE)

    assert len(completed) == 1
    project = completed[0]
    assert project["name"] == "Alpha"
    lines = content.splitlines()
    assert lines[project["start_line"]] == "## Project: Alpha"
    assert lines[project["end_line"]] == "---"
    assert project["content"].startswith("## Project: Alpha")


def test_detect_completed_projects_mixed() -> None:
    content = (
        "# Plans\n"
        "\n"
        "## Project: Alpha\n"
        "### Phase 1: Done `cc:done`\n"
        "---\n"
        "\n"
        "## Project: Beta\n"
        "### Phase 1: Work `cc:WIP`\n"
        "- `cc:WIP` in progress\n"
    )

    completed = detect_completed_projects(content, DEFAULT_MARKER_PATTERN, DEFAULT_MARKER_TO_STATE)

    assert [p["name"] for p in completed] == ["Alpha"]


def test_detect_completed_projects_no_phase_marker() -> None:
    content = (
        "# Plans\n"
        "\n"
        "## Project: Markerless\n"
        "### Phase 1: Setup\n"
        "- `cc:done` prepare\n"
        "- `cc:done` validate\n"
        "### Phase 2: Release\n"
        "- `cc:done` publish\n"
    )

    completed = detect_completed_projects(content, DEFAULT_MARKER_PATTERN, DEFAULT_MARKER_TO_STATE)

    assert len(completed) == 1
    assert completed[0]["name"] == "Markerless"


def test_detect_completed_projects_empty() -> None:
    content = "# Plans\n\n## Project: NoPhase\n- `cc:done` orphan task\n"

    completed = detect_completed_projects(content, DEFAULT_MARKER_PATTERN, DEFAULT_MARKER_TO_STATE)

    assert completed == []


def test_detect_completed_projects_empty_phase_not_done() -> None:
    """フェーズにタスクが0件の場合は完了扱いにしない（Critical fix）。"""
    content = (
        "# Plans\n"
        "\n"
        "## Project: EmptyPhase\n"
        "### Phase 1: Done `cc:done`\n"
        "- `cc:done` task1\n"
        "### Phase 2: Planned\n"
        "#### TODO\n"
    )

    completed = detect_completed_projects(content, DEFAULT_MARKER_PATTERN, DEFAULT_MARKER_TO_STATE)

    assert completed == []


def test_archive_projects_single(tmp_path: Path) -> None:
    plans_path = tmp_path / "Plans.md"
    archive_path = tmp_path / "Plans.archive.md"
    content = (
        "# Plans\n"
        "\n"
        "## Project: Alpha\n"
        "### Phase 1: Done `cc:done`\n"
        "---\n"
        "\n"
        "## Project: Beta\n"
        "### Phase 1: Work `cc:TODO`\n"
        "- `cc:TODO` pending\n"
    )
    plans_path.write_text(content, encoding="utf-8")
    completed = detect_completed_projects(content, DEFAULT_MARKER_PATTERN, DEFAULT_MARKER_TO_STATE)

    updated = archive_projects(plans_path, archive_path, completed, content)

    assert "## Project: Alpha" not in updated
    assert "## Project: Beta" in updated
    archive_text = archive_path.read_text(encoding="utf-8")
    assert "# Archived Plans" in archive_text
    assert "## Project: Alpha" in archive_text
    assert re.search(r"## Archived: \d{4}-\d{2}-\d{2}", archive_text)


def test_archive_projects_multiple(tmp_path: Path) -> None:
    plans_path = tmp_path / "Plans.md"
    archive_path = tmp_path / "Plans.archive.md"
    content = (
        "# Plans\n"
        "\n"
        "## Project: Alpha\n"
        "### Phase 1: Done `cc:done`\n"
        "---\n"
        "\n"
        "## Project: Beta\n"
        "### Phase 1: Done `cc:done`\n"
        "---\n"
        "\n"
        "## Project: Gamma\n"
        "### Phase 1: Work `cc:TODO`\n"
        "- `cc:TODO` remain\n"
    )
    plans_path.write_text(content, encoding="utf-8")
    completed = detect_completed_projects(content, DEFAULT_MARKER_PATTERN, DEFAULT_MARKER_TO_STATE)

    updated = archive_projects(plans_path, archive_path, completed, content)

    assert "## Project: Alpha" not in updated
    assert "## Project: Beta" not in updated
    assert "## Project: Gamma" in updated
    archive_text = archive_path.read_text(encoding="utf-8")
    assert "## Project: Alpha" in archive_text
    assert "## Project: Beta" in archive_text
    assert archive_text.count("## Archived: ") == 2


def test_archive_projects_all_completed_with_decisions(tmp_path: Path) -> None:
    plans_path = tmp_path / "Plans.md"
    archive_path = tmp_path / "Plans.archive.md"
    content = (
        "# Plans\n"
        "\n"
        "## Project: Alpha\n"
        "### Phase 1: Done `cc:done`\n"
        "---\n"
        "\n"
        "## Decisions\n"
        "- 2026-03-14: keep API\n"
        "\n"
        "## Notes\n"
        "- archived with projects\n"
    )
    plans_path.write_text(content, encoding="utf-8")
    completed = detect_completed_projects(content, DEFAULT_MARKER_PATTERN, DEFAULT_MARKER_TO_STATE)

    updated = archive_projects(plans_path, archive_path, completed, content)

    assert "## Project:" not in updated
    assert "## Decisions" not in updated
    assert "## Notes" not in updated
    archive_text = archive_path.read_text(encoding="utf-8")
    assert "## Decisions" in archive_text
    assert "## Notes" in archive_text


def test_archive_projects_partial_with_decisions(tmp_path: Path) -> None:
    plans_path = tmp_path / "Plans.md"
    archive_path = tmp_path / "Plans.archive.md"
    content = (
        "# Plans\n"
        "\n"
        "## Project: Alpha\n"
        "### Phase 1: Done `cc:done`\n"
        "---\n"
        "\n"
        "## Project: Beta\n"
        "### Phase 1: Work `cc:TODO`\n"
        "- `cc:TODO` pending\n"
        "---\n"
        "\n"
        "## Decisions\n"
        "- 2026-03-14: keep API\n"
        "\n"
        "## Notes\n"
        "- keep until all complete\n"
    )
    plans_path.write_text(content, encoding="utf-8")
    completed = detect_completed_projects(content, DEFAULT_MARKER_PATTERN, DEFAULT_MARKER_TO_STATE)

    updated = archive_projects(plans_path, archive_path, completed, content)

    assert "## Project: Alpha" not in updated
    assert "## Project: Beta" in updated
    assert "## Decisions" in updated
    assert "## Notes" in updated
    archive_text = archive_path.read_text(encoding="utf-8")
    assert "## Project: Alpha" in archive_text
    assert "## Decisions" not in archive_text
    assert "## Notes" not in archive_text


def test_archive_appends_to_existing(tmp_path: Path) -> None:
    plans_path = tmp_path / "Plans.md"
    archive_path = tmp_path / "Plans.archive.md"
    archive_path.write_text(
        "# Archived Plans\n\n## Archived: 2026-03-01\n\n## Project: Old\n\n---\n",
        encoding="utf-8",
    )
    content = "# Plans\n\n## Project: New\n### Phase 1: Done `cc:done`\n"
    plans_path.write_text(content, encoding="utf-8")
    completed = detect_completed_projects(content, DEFAULT_MARKER_PATTERN, DEFAULT_MARKER_TO_STATE)

    _ = archive_projects(plans_path, archive_path, completed, content)

    archive_text = archive_path.read_text(encoding="utf-8")
    assert archive_text.count("# Archived Plans") == 1
    assert "## Project: Old" in archive_text
    assert "## Project: New" in archive_text


def test_archive_creates_new_file(tmp_path: Path) -> None:
    plans_path = tmp_path / "Plans.md"
    archive_path = tmp_path / "Plans.archive.md"
    content = "# Plans\n\n## Project: Alpha\n### Phase 1: Done `cc:done`\n"
    plans_path.write_text(content, encoding="utf-8")
    completed = detect_completed_projects(content, DEFAULT_MARKER_PATTERN, DEFAULT_MARKER_TO_STATE)

    _ = archive_projects(plans_path, archive_path, completed, content)

    assert archive_path.exists()
    archive_text = archive_path.read_text(encoding="utf-8")
    assert archive_text.startswith("# Archived Plans")
    assert "## Project: Alpha" in archive_text


def test_main_archives_even_when_summary_disabled(tmp_path: Path, monkeypatch) -> None:
    plans_path = tmp_path / ".claude" / "Plans.md"
    plans_path.parent.mkdir(parents=True, exist_ok=True)
    plans_path.write_text(
        "# Plans\n\n## Project: Alpha\n### Phase 1: Done `cc:done`\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(_mod, "read_hook_input", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(
        _mod,
        "load_config",
        lambda _project_dir: {
            "plans_file": ".claude/Plans.md",
            "show_summary_on_start": False,
            "max_display_tasks": 20,
            "markers": dict(DEFAULT_MARKERS),
        },
    )
    monkeypatch.setattr(sys, "stdout", StringIO())

    _mod.main()

    archive_path = tmp_path / ".claude" / "Plans.archive.md"
    assert archive_path.is_file()
    assert "## Project: Alpha" in archive_path.read_text(encoding="utf-8")


def test_main_uses_custom_markers_for_archive_detection(tmp_path: Path, monkeypatch) -> None:
    plans_path = tmp_path / ".claude" / "Plans.md"
    plans_path.parent.mkdir(parents=True, exist_ok=True)
    plans_path.write_text(
        "# Plans\n\n## Project: Custom\n### Phase 1: Done `custom:done`\n",
        encoding="utf-8",
    )

    custom_markers = {
        "todo": "custom:TODO",
        "wip": "custom:WIP",
        "done": "custom:done",
        "blocked": "custom:blocked",
    }
    monkeypatch.setattr(_mod, "read_hook_input", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(
        _mod,
        "load_config",
        lambda _project_dir: {
            "plans_file": ".claude/Plans.md",
            "show_summary_on_start": False,
            "max_display_tasks": 20,
            "markers": custom_markers,
        },
    )
    monkeypatch.setattr(sys, "stdout", StringIO())

    _mod.main()

    archive_path = tmp_path / ".claude" / "Plans.archive.md"
    assert archive_path.is_file()
    archive_text = archive_path.read_text(encoding="utf-8")
    assert "## Project: Custom" in archive_text
