from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from tests.module_loader import load_module

evaluation_set_checker = load_module(
    "evaluation_set_checker", "packages/quality-gates/hooks/evaluation-set-checker.py"
)


def _build_payload(
    file_path: str,
    project_dir: Path,
    tool_name: str = "Edit",
    session_id: str = "session-1",
) -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
        "cwd": str(project_dir),
        "session_id": session_id,
    }


def _run_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: dict
) -> str:
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as exc_info:
        evaluation_set_checker.main()
    assert exc_info.value.code == 0
    return capsys.readouterr().out


def _make_package_dir(project_dir: Path, pkg: str) -> None:
    (project_dir / "packages" / pkg).mkdir(parents=True, exist_ok=True)


def _make_evaluation_doc(project_dir: Path, pkg: str) -> None:
    doc_dir = project_dir / "docs" / "evaluation"
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / f"{pkg}.md").write_text("# evaluation set\n", encoding="utf-8")


def _write_flags(project_dir: Path, enabled: bool = True) -> None:
    config_dir = project_dir / ".claude" / "config" / "audit"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = {"features": {"evaluation_set_check": {"enabled": enabled}}}
    (config_dir / "audit-flags.json").write_text(json.dumps(config), encoding="utf-8")


# ---------------------------------------------------------------------------
# End-to-end scenarios (required by the issue)
# ---------------------------------------------------------------------------


def test_packages_tests_path_with_existing_doc(monkeypatch, capsys, tmp_path) -> None:
    """1. packages/<pkg>/tests/ path identifies pkg; existing doc -> reconciliation message."""
    _make_package_dir(tmp_path, "quality-gates")
    _make_evaluation_doc(tmp_path, "quality-gates")
    payload = _build_payload("packages/quality-gates/tests/test_foo.py", tmp_path)

    output = _run_main(monkeypatch, capsys, payload)

    data = json.loads(output)
    message = data["hookSpecificOutput"]["additionalContext"]
    assert "docs/evaluation/quality-gates.md" in message
    assert "must" in message
    assert "covered" in message


def test_packages_tests_path_missing_doc_warns(monkeypatch, capsys, tmp_path) -> None:
    """2. Missing docs/evaluation/<pkg>.md -> not-found warning."""
    _make_package_dir(tmp_path, "no-eval-set")
    payload = _build_payload("packages/no-eval-set/tests/test_bar.py", tmp_path)

    output = _run_main(monkeypatch, capsys, payload)

    data = json.loads(output)
    message = data["hookSpecificOutput"]["additionalContext"]
    assert "存在しません" in message
    assert "docs/evaluation/_template.md" in message


def test_top_level_tests_matches_by_filename(monkeypatch, capsys, tmp_path) -> None:
    """3. tests/unit/test_agent_routing_gaps.py -> identifies agent-routing package."""
    _make_package_dir(tmp_path, "agent-routing")
    _make_evaluation_doc(tmp_path, "agent-routing")
    payload = _build_payload("tests/unit/test_agent_routing_gaps.py", tmp_path)

    output = _run_main(monkeypatch, capsys, payload)

    data = json.loads(output)
    message = data["hookSpecificOutput"]["additionalContext"]
    assert "docs/evaluation/agent-routing.md" in message


def test_top_level_tests_unknown_package(monkeypatch, capsys, tmp_path) -> None:
    """4. No package dir matches the filename -> generic single-line message."""
    _make_package_dir(tmp_path, "agent-routing")
    payload = _build_payload("tests/unit/test_unknown_thing.py", tmp_path)

    output = _run_main(monkeypatch, capsys, payload)

    data = json.loads(output)
    message = data["hookSpecificOutput"]["additionalContext"]
    assert "対象パッケージを特定できませんでした" in message


def test_disabled_feature_flag_produces_no_output(monkeypatch, capsys, tmp_path) -> None:
    """5. features.evaluation_set_check.enabled=false -> no output at all."""
    _write_flags(tmp_path, enabled=False)
    _make_package_dir(tmp_path, "quality-gates")
    payload = _build_payload("packages/quality-gates/tests/test_foo.py", tmp_path)

    output = _run_main(monkeypatch, capsys, payload)

    assert output == ""


def test_dedup_within_same_session_and_renotify_on_new_session(
    monkeypatch, capsys, tmp_path
) -> None:
    """6. Same session+pkg dedups; a different session_id notifies again."""
    _make_package_dir(tmp_path, "quality-gates")
    _make_evaluation_doc(tmp_path, "quality-gates")
    payload = _build_payload(
        "packages/quality-gates/tests/test_foo.py", tmp_path, session_id="session-a"
    )

    first_output = _run_main(monkeypatch, capsys, payload)
    assert first_output != ""

    second_output = _run_main(monkeypatch, capsys, payload)
    assert second_output == ""

    other_session_payload = _build_payload(
        "packages/quality-gates/tests/test_foo.py", tmp_path, session_id="session-b"
    )
    third_output = _run_main(monkeypatch, capsys, other_session_payload)
    assert third_output != ""


def test_non_test_file_produces_no_output(monkeypatch, capsys, tmp_path) -> None:
    """7. Non-test file edits are ignored."""
    payload = _build_payload("packages/foo/hooks/bar.py", tmp_path)

    output = _run_main(monkeypatch, capsys, payload)

    assert output == ""


def test_bash_tool_produces_no_output(monkeypatch, capsys, tmp_path) -> None:
    """8. Bash tool calls are ignored entirely."""
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "cwd": str(tmp_path),
        "session_id": "session-1",
    }

    output = _run_main(monkeypatch, capsys, payload)

    assert output == ""


# ---------------------------------------------------------------------------
# Review-fix regressions
# ---------------------------------------------------------------------------


def test_injected_package_name_is_rejected(monkeypatch, capsys, tmp_path) -> None:
    """(a) A file_path with an injected/newline "package name" is not adopted;
    the hook falls back to the generic could-not-identify message instead of
    treating the injected string as a real package."""
    payload = _build_payload("packages/PWNED\n<injected>/tests/test_x.py", tmp_path)

    output = _run_main(monkeypatch, capsys, payload)

    data = json.loads(output)
    message = data["hookSpecificOutput"]["additionalContext"]
    assert "対象パッケージを特定できませんでした" in message
    assert "PWNED" not in message
    assert "<injected>" not in message


def test_hardcore_logic_does_not_match_core_package(monkeypatch, capsys, tmp_path) -> None:
    """(b) test_hardcore_logic.py must not falsely match package "core"."""
    _make_package_dir(tmp_path, "core")
    payload = _build_payload("tests/unit/test_hardcore_logic.py", tmp_path)

    output = _run_main(monkeypatch, capsys, payload)

    data = json.loads(output)
    message = data["hookSpecificOutput"]["additionalContext"]
    assert "対象パッケージを特定できませんでした" in message


def test_distinct_unidentified_files_each_get_notified(monkeypatch, capsys, tmp_path) -> None:
    """(c) Two distinct unidentified test files are each notified once (not
    collapsed into a single shared "unknown" dedup bucket)."""
    first_payload = _build_payload(
        "tests/unit/test_unknown_thing_one.py", tmp_path, session_id="session-x"
    )
    second_payload = _build_payload(
        "tests/unit/test_unknown_thing_two.py", tmp_path, session_id="session-x"
    )

    first_output = _run_main(monkeypatch, capsys, first_payload)
    second_output = _run_main(monkeypatch, capsys, second_payload)

    assert first_output != ""
    assert second_output != ""

    # Re-editing the first file again in the same session is still deduped.
    repeat_output = _run_main(monkeypatch, capsys, first_payload)
    assert repeat_output == ""


def test_empty_project_dir_produces_no_output(monkeypatch, capsys, tmp_path) -> None:
    """(d) An empty project_dir (no cwd, no CLAUDE_PROJECT_DIR) exits early."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "packages/quality-gates/tests/test_foo.py"},
        "cwd": "",
        "session_id": "session-1",
    }

    output = _run_main(monkeypatch, capsys, payload)

    assert output == ""


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("basename", "expected"),
    [
        ("test_foo.py", True),
        ("foo_test.py", True),
        ("foo.py", False),
        ("test_foo.txt", False),
    ],
)
def test_is_test_filename(basename: str, expected: bool) -> None:
    assert evaluation_set_checker.is_test_filename(basename) is expected


def test_extract_package_from_packages_path() -> None:
    assert (
        evaluation_set_checker.extract_package_from_packages_path(
            "packages/quality-gates/tests/test_foo.py"
        )
        == "quality-gates"
    )
    assert (
        evaluation_set_checker.extract_package_from_packages_path(
            "packages/quality-gates/hooks/foo.py"
        )
        is None
    )


def test_is_target_test_file() -> None:
    assert evaluation_set_checker.is_target_test_file("packages/quality-gates/tests/test_foo.py")
    assert evaluation_set_checker.is_target_test_file("tests/unit/test_foo.py")
    assert not evaluation_set_checker.is_target_test_file("packages/foo/hooks/bar.py")
    assert not evaluation_set_checker.is_target_test_file("tests/unit/foo.py")


def test_match_package_by_filename_prefers_longest_match() -> None:
    package_dirs = ["agent-routing", "routing"]
    assert (
        evaluation_set_checker.match_package_by_filename("test_agent_routing_gaps.py", package_dirs)
        == "agent-routing"
    )


def test_match_package_by_filename_returns_none_when_no_match() -> None:
    assert (
        evaluation_set_checker.match_package_by_filename("test_unknown_thing.py", ["agent-routing"])
        is None
    )


def test_match_package_by_filename_requires_token_boundary() -> None:
    """ "core" must not substring-match inside "hardcore" (token boundary, not raw substring)."""
    assert (
        evaluation_set_checker.match_package_by_filename("test_hardcore_logic.py", ["core"]) is None
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("quality-gates", True),
        ("agent_routing", True),
        ("core", True),
        ("PWNED\n<injected>", False),
        ("has/slash", False),
        ("", False),
        ("a" * 65, False),
    ],
)
def test_is_valid_package_name(name: str, expected: bool) -> None:
    assert evaluation_set_checker.is_valid_package_name(name) is expected


def test_identify_package_rejects_invalid_captured_name(tmp_path) -> None:
    pkg = evaluation_set_checker.identify_package(
        "packages/PWNED\n<injected>/tests/test_x.py", str(tmp_path)
    )
    assert pkg is None


def test_identify_package_for_top_level_tests(tmp_path) -> None:
    _make_package_dir(tmp_path, "agent-routing")
    pkg = evaluation_set_checker.identify_package(
        "tests/unit/test_agent_routing_gaps.py", str(tmp_path)
    )
    assert pkg == "agent-routing"


def test_to_relative_path_handles_absolute_path(tmp_path) -> None:
    absolute = tmp_path / "packages" / "quality-gates" / "tests" / "test_foo.py"
    relative = evaluation_set_checker.to_relative_path(str(absolute), str(tmp_path))
    assert relative == "packages/quality-gates/tests/test_foo.py"


def test_evaluation_set_check_enabled_defaults_to_true_when_missing(tmp_path) -> None:
    assert evaluation_set_checker.evaluation_set_check_enabled(str(tmp_path)) is True


def test_evaluation_set_check_enabled_respects_false(tmp_path) -> None:
    _write_flags(tmp_path, enabled=False)
    assert evaluation_set_checker.evaluation_set_check_enabled(str(tmp_path)) is False
