from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from tests.module_loader import load_module

test_tampering_detector = load_module(
    "test_tampering_detector", "packages/quality-gates/hooks/test-tampering-detector.py"
)


@pytest.fixture()
def _clean_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(test_tampering_detector, "STATE_FILE", tmp_path / "tampering-state.json")


def test_is_test_file_detects_python_and_javascript_patterns() -> None:
    assert test_tampering_detector.is_test_file("tests/test_auth.py")
    assert test_tampering_detector.is_test_file("src/foo_test.py")
    assert test_tampering_detector.is_test_file("frontend/button.test.tsx")
    assert test_tampering_detector.is_test_file("frontend/__tests__/button.ts")
    assert not test_tampering_detector.is_test_file("src/main.py")


def test_extract_added_lines_ignores_diff_metadata() -> None:
    diff = "\n".join(
        [
            "diff --git a/foo.py b/foo.py",
            "--- a/foo.py",
            "+++ b/foo.py",
            "@@ -1 +1,2 @@",
            "+@pytest.mark.skip",
            "+def test_example():",
        ]
    )

    assert test_tampering_detector.extract_added_lines(diff) == [
        "@pytest.mark.skip",
        "def test_example():",
    ]


def test_scan_added_lines_detects_skip_and_disable_patterns() -> None:
    findings = test_tampering_detector.scan_added_lines(
        "tests/test_auth.py",
        [
            "@pytest.mark.skip",
            "# type: ignore[attr-defined]",
            "const x = 1; // eslint-disable-line no-console",
        ],
    )

    assert [finding["label"] for finding in findings] == [
        "@pytest.mark.skip / @unittest.skip",
        "type: ignore",
        "eslint-disable",
    ]


def test_scan_added_lines_ignores_suppression_patterns_in_non_test_file() -> None:
    findings = test_tampering_detector.scan_added_lines(
        "src/main.py",
        [
            "# type: ignore[attr-defined]",
            "# noqa: F401",
            "const x = 1; // eslint-disable-line no-console",
        ],
    )

    assert findings == []


def test_extract_deleted_test_files_filters_non_test_paths() -> None:
    name_status = "\n".join(
        [
            "D\ttests/test_auth.py",
            "D\tsrc/main.py",
            "M\tfrontend/app.tsx",
            "D\tfrontend/button.test.tsx",
        ]
    )

    assert test_tampering_detector.extract_deleted_test_files(name_status) == [
        "frontend/button.test.tsx",
        "tests/test_auth.py",
    ]


def test_collect_tampering_findings_for_new_file_uses_tool_input(monkeypatch, _clean_state) -> None:
    payload = {
        "tool_name": "Write",
        "cwd": "/repo",
        "tool_input": {
            "file_path": "tests/test_auth.py",
            "content": "@pytest.mark.skip\n# noqa: F401\n",
        },
    }

    monkeypatch.setattr(test_tampering_detector, "run_git_command", lambda *args: "")
    findings = test_tampering_detector.collect_tampering_findings(payload)

    assert [finding["label"] for finding in findings] == [
        "@pytest.mark.skip / @unittest.skip",
        "noqa",
    ]


def test_collect_tampering_findings_detects_deleted_test_files(monkeypatch, _clean_state) -> None:
    payload = {
        "tool_name": "Bash",
        "cwd": "/repo",
        "tool_input": {"command": "git rm tests/test_auth.py"},
    }

    monkeypatch.setattr(
        test_tampering_detector,
        "run_git_command",
        lambda _project_dir, *args: (
            "D\ttests/test_auth.py\nD\ttests/test_other.py\n" if "--name-status" in args else ""
        ),
    )

    findings = test_tampering_detector.collect_tampering_findings(payload)

    assert findings == [
        {
            "type": "deleted_test_file",
            "file_path": "tests/test_auth.py",
            "label": "deleted test file",
            "snippet": "",
        }
    ]


def test_extract_delete_targets_handles_delete_and_git_rm_commands() -> None:
    assert test_tampering_detector.extract_delete_targets(
        "Delete", {"file_path": "/repo/tests/test_auth.py"}, "/repo"
    ) == ["tests/test_auth.py"]
    assert test_tampering_detector.extract_delete_targets(
        "Bash", {"command": "git rm -f tests testdata/fixture.py"}, "/repo"
    ) == ["tests", "testdata/fixture.py"]
    assert test_tampering_detector.extract_delete_targets(
        "Bash", {"command": 'bash -lc "git rm tests/*.py"'}, "/repo"
    ) == ["tests/*.py"]
    assert test_tampering_detector.extract_delete_targets(
        "Bash", {"command": "rm tests/a.py && rm tests/b.py"}, "/repo"
    ) == ["tests/a.py", "tests/b.py"]
    assert test_tampering_detector.extract_delete_targets(
        "Bash", {"command": "rm tests/a.py; rm tests/b.py"}, "/repo"
    ) == ["tests/a.py", "tests/b.py"]


def test_get_deleted_test_files_filters_to_current_targets(monkeypatch) -> None:
    monkeypatch.setattr(
        test_tampering_detector,
        "run_git_command",
        lambda _project_dir, *args: "D\ttests/test_auth.py\nD\ttests/sub/test_other.py\n",
    )

    assert test_tampering_detector.get_deleted_test_files("/repo", ["tests/test_auth.py"]) == [
        "tests/test_auth.py"
    ]
    assert test_tampering_detector.get_deleted_test_files("/repo", ["tests"]) == [
        "tests/sub/test_other.py",
        "tests/test_auth.py",
    ]


def test_get_deleted_test_files_matches_glob_targets(monkeypatch) -> None:
    monkeypatch.setattr(
        test_tampering_detector,
        "run_git_command",
        lambda _project_dir, *args: "D\ttests/test_auth.py\nD\ttests/helpers/util.txt\n",
    )

    assert test_tampering_detector.get_deleted_test_files("/repo", ["tests/*.py"]) == [
        "tests/test_auth.py"
    ]


def test_path_glob_match_is_anchored() -> None:
    assert test_tampering_detector._match_path_glob("tests/test_auth.py", "tests/*.py")
    assert not test_tampering_detector._match_path_glob("src/tests/test_auth.py", "tests/*.py")


def test_get_unreported_deleted_test_files_suppresses_repeats(monkeypatch, _clean_state) -> None:
    monkeypatch.setattr(
        test_tampering_detector,
        "get_all_deleted_test_files",
        lambda _project_dir: ["tests/test_auth.py", "tests/test_other.py"],
    )
    monkeypatch.setattr(
        test_tampering_detector,
        "get_deleted_test_files",
        lambda _project_dir, _targets: ["tests/test_auth.py", "tests/test_other.py"],
    )

    first = test_tampering_detector.get_unreported_deleted_test_files("/repo", ["tests/*.py"])
    second = test_tampering_detector.get_unreported_deleted_test_files("/repo", ["tests/*.py"])

    assert first == ["tests/test_auth.py", "tests/test_other.py"]
    assert second == []


def test_get_project_state_key_prefers_git_common_dir(monkeypatch) -> None:
    monkeypatch.setattr(
        test_tampering_detector,
        "run_git_command",
        lambda _project_dir, *args: (
            "../../.git\n" if args == ("rev-parse", "--git-common-dir") else ""
        ),
    )

    key = test_tampering_detector.get_project_state_key("/repo/.worktrees/feat-4")

    assert key.endswith("/repo/.git")


def test_collect_tampering_findings_suppresses_repeated_pattern_warnings(
    monkeypatch, _clean_state
) -> None:
    payload = {
        "tool_name": "Write",
        "cwd": "/repo",
        "tool_input": {"file_path": "tests/test_auth.py", "content": "@pytest.mark.skip\n"},
    }

    monkeypatch.setattr(test_tampering_detector, "run_git_command", lambda *args: "")

    first = test_tampering_detector.collect_tampering_findings(payload)
    second = test_tampering_detector.collect_tampering_findings(payload)

    assert [finding["label"] for finding in first] == ["@pytest.mark.skip / @unittest.skip"]
    assert second == []


def test_main_outputs_warning(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "tests/test_auth.py", "content": "it.skip('x')\n"},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    monkeypatch.setattr(
        test_tampering_detector,
        "collect_tampering_findings",
        lambda _data: [
            {
                "type": "pattern",
                "file_path": "tests/test_auth.py",
                "label": "it.skip() / test.skip() / describe.skip()",
                "snippet": "it.skip('x')",
            }
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        test_tampering_detector.main()

    assert exc_info.value.code == 0
    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[Warning]" in context
    assert "tests/test_auth.py" in context
    assert "it.skip()" in context
