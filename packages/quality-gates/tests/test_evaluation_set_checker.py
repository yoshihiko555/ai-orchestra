from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest
import yaml

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


def _write_mapping_config(project_dir: Path, mappings: list[dict]) -> None:
    """Write .claude/config/quality-gates/evaluation-set-mapping.yaml.

    Writing the project-level override (rather than relying on the packages/
    quality-gates/config/ fallback) keeps tests isolated from whatever
    AI_ORCHESTRA_DIR happens to point at in the running environment.
    """
    config_dir = project_dir / ".claude" / "config" / "quality-gates"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = {"mappings": mappings}
    (config_dir / "evaluation-set-mapping.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )


def _write_local_mapping_config(project_dir: Path, mappings: list[dict]) -> None:
    """Write .claude/config/quality-gates/evaluation-set-mapping.local.yaml."""
    config_dir = project_dir / ".claude" / "config" / "quality-gates"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = {"mappings": mappings}
    (config_dir / "evaluation-set-mapping.local.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )


def _write_raw_mapping_config(project_dir: Path, text: str) -> None:
    """Write a raw (possibly malformed) evaluation-set-mapping.yaml body."""
    config_dir = project_dir / ".claude" / "config" / "quality-gates"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "evaluation-set-mapping.yaml").write_text(text, encoding="utf-8")


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


def test_main_normalizes_subdirectory_before_disabled_config_lookup(
    monkeypatch, capsys, tmp_path
) -> None:
    """subdirectory cwd でも project root の feature flag を参照する。"""
    repo_root = tmp_path / "repo"
    (repo_root / ".claude").mkdir(parents=True)
    subdirectory = repo_root / "packages" / "quality-gates"
    subdirectory.mkdir(parents=True)
    test_file = subdirectory / "tests" / "test_foo.py"
    config_calls = []

    def _load_config(package_name: str, filename: str, project_dir: str) -> dict:
        config_calls.append((package_name, filename, project_dir))
        return {"features": {"evaluation_set_check": {"enabled": False}}}

    monkeypatch.setattr(evaluation_set_checker, "load_package_config", _load_config)
    payload = _build_payload(str(test_file), subdirectory)

    output = _run_main(monkeypatch, capsys, payload)

    assert output == ""
    assert config_calls == [("audit", "audit-flags.json", str(repo_root))]
    state_file = repo_root / ".claude" / "state" / evaluation_set_checker.STATE_FILENAME
    assert not state_file.exists()


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


def test_explicit_mapping_overrides_core_false_match(monkeypatch, capsys, tmp_path) -> None:
    """Issue #237: test_orchestra_manager_core.py must route to the explicit
    mapping's package (orchex-cli), not fall through to the filename-token
    heuristic's "core" false match."""
    _make_package_dir(tmp_path, "core")
    _make_evaluation_doc(tmp_path, "orchex-cli")
    _write_mapping_config(
        tmp_path,
        [{"package": "orchex-cli", "test_globs": ["tests/unit/test_orchestra_manager_*.py"]}],
    )
    payload = _build_payload("tests/unit/test_orchestra_manager_core.py", tmp_path)

    output = _run_main(monkeypatch, capsys, payload)

    data = json.loads(output)
    message = data["hookSpecificOutput"]["additionalContext"]
    assert "docs/evaluation/orchex-cli.md" in message


def test_explicit_mapping_identifies_ssot_without_packages_dir(
    monkeypatch, capsys, tmp_path
) -> None:
    """Issue #237: a test file for an SSOT target with no packages/<pkg>/
    directory (e.g. orchex CLI) is identified via the explicit mapping."""
    _make_evaluation_doc(tmp_path, "orchex-cli")
    _write_mapping_config(
        tmp_path,
        [{"package": "orchex-cli", "test_globs": ["tests/unit/test_ai_orchestra_cli.py"]}],
    )
    payload = _build_payload("tests/unit/test_ai_orchestra_cli.py", tmp_path)

    output = _run_main(monkeypatch, capsys, payload)

    data = json.loads(output)
    message = data["hookSpecificOutput"]["additionalContext"]
    assert "docs/evaluation/orchex-cli.md" in message


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


# ---------------------------------------------------------------------------
# Explicit evaluation-set-mapping.yaml (Issue #237)
# ---------------------------------------------------------------------------


def test_match_explicit_mapping_matches_glob() -> None:
    mappings = [{"package": "orchex-cli", "test_globs": ["tests/unit/test_orchestra_manager_*.py"]}]
    pkg = evaluation_set_checker.match_explicit_mapping(
        "tests/unit/test_orchestra_manager_core.py", mappings
    )
    assert pkg == "orchex-cli"


def test_match_explicit_mapping_returns_none_when_no_pattern_matches() -> None:
    mappings = [{"package": "orchex-cli", "test_globs": ["tests/unit/test_orchestra_manager_*.py"]}]
    pkg = evaluation_set_checker.match_explicit_mapping(
        "tests/unit/test_unrelated_thing.py", mappings
    )
    assert pkg is None


def test_match_explicit_mapping_is_case_sensitive() -> None:
    """fnmatchcase (not fnmatch) is used, so casing differences don't match."""
    mappings = [{"package": "orchex-cli", "test_globs": ["Tests/Unit/*.py"]}]
    pkg = evaluation_set_checker.match_explicit_mapping("tests/unit/test_x.py", mappings)
    assert pkg is None


def test_load_evaluation_set_mapping_missing_config_returns_empty(monkeypatch, tmp_path) -> None:
    # Isolate from the real repo's packages/quality-gates/config/ fallback,
    # which find_package_config() would otherwise consult via AI_ORCHESTRA_DIR.
    monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
    assert evaluation_set_checker.load_evaluation_set_mapping(str(tmp_path)) == []


def test_load_evaluation_set_mapping_skips_malformed_entries(tmp_path) -> None:
    _write_mapping_config(
        tmp_path,
        [
            {"package": "orchex-cli", "test_globs": ["tests/unit/test_ai_orchestra_cli.py"]},
            {"package": "no-globs-key"},
            {"test_globs": ["tests/unit/*.py"]},
            "not-a-dict",
        ],
    )
    mappings = evaluation_set_checker.load_evaluation_set_mapping(str(tmp_path))
    assert mappings == [
        {"package": "orchex-cli", "test_globs": ["tests/unit/test_ai_orchestra_cli.py"]}
    ]


def test_load_evaluation_set_mapping_malformed_root_does_not_raise(monkeypatch, tmp_path) -> None:
    """PR #243 review: a .yaml whose document root is a bare list (e.g. a user
    forgetting the `mappings:` key) must not raise AttributeError."""
    monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
    _write_raw_mapping_config(tmp_path, "- package: orchex-cli\n- test_globs: [x]\n")
    assert evaluation_set_checker.load_evaluation_set_mapping(str(tmp_path)) == []


def test_load_evaluation_set_mapping_local_addition_preserves_base_entries(tmp_path) -> None:
    """PR #243 review: a project-local mapping addition must not silently drop
    shipped base entries (e.g. orchex-cli) via a naive whole-list replace."""
    _write_mapping_config(
        tmp_path,
        [{"package": "orchex-cli", "test_globs": ["tests/unit/test_orchestra_manager_*.py"]}],
    )
    _write_local_mapping_config(
        tmp_path,
        [{"package": "my-project", "test_globs": ["tests/unit/test_my_project_*.py"]}],
    )

    mappings = evaluation_set_checker.load_evaluation_set_mapping(str(tmp_path))

    assert {entry["package"] for entry in mappings} == {"orchex-cli", "my-project"}


def test_load_evaluation_set_mapping_local_override_replaces_same_package_entry(
    tmp_path,
) -> None:
    """A local entry for the same `package` as a base entry replaces it (rather
    than being appended alongside it or merging test_globs)."""
    _write_mapping_config(
        tmp_path,
        [{"package": "orchex-cli", "test_globs": ["tests/unit/test_orchestra_manager_*.py"]}],
    )
    _write_local_mapping_config(
        tmp_path,
        [{"package": "orchex-cli", "test_globs": ["tests/unit/test_custom_*.py"]}],
    )

    mappings = evaluation_set_checker.load_evaluation_set_mapping(str(tmp_path))

    assert mappings == [{"package": "orchex-cli", "test_globs": ["tests/unit/test_custom_*.py"]}]


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


def test_identify_package_explicit_mapping_takes_priority_over_packages_dir(tmp_path) -> None:
    """Issue #237: an explicit mapping entry wins even when the file also sits
    under a matching packages/<pkg>/tests/ directory."""
    _make_package_dir(tmp_path, "quality-gates")
    _write_mapping_config(
        tmp_path,
        [{"package": "other-set", "test_globs": ["packages/quality-gates/tests/test_foo.py"]}],
    )
    pkg = evaluation_set_checker.identify_package(
        "packages/quality-gates/tests/test_foo.py", str(tmp_path)
    )
    assert pkg == "other-set"


def test_to_relative_path_handles_absolute_path(tmp_path) -> None:
    absolute = tmp_path / "packages" / "quality-gates" / "tests" / "test_foo.py"
    relative = evaluation_set_checker.to_relative_path(str(absolute), str(tmp_path))
    assert relative == "packages/quality-gates/tests/test_foo.py"


def test_evaluation_set_check_enabled_defaults_to_true_when_missing() -> None:
    assert evaluation_set_checker.evaluation_set_check_enabled({}) is True


def test_evaluation_set_check_enabled_respects_false() -> None:
    config = {"features": {"evaluation_set_check": {"enabled": False}}}
    assert evaluation_set_checker.evaluation_set_check_enabled(config) is False


# ---------------------------------------------------------------------------
# Ordering / config-read-once regressions (code review: cheap checks before
# expensive config read; audit-flags.json read only once per invocation)
# ---------------------------------------------------------------------------


def test_config_not_read_for_non_test_files(monkeypatch, capsys, tmp_path) -> None:
    """Non-test files short-circuit via is_target_test_file() before any
    audit-flags.json read happens (cheap check first)."""

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("load_package_config must not be called for non-test files")

    monkeypatch.setattr(evaluation_set_checker, "load_package_config", _fail_if_called)
    payload = _build_payload("packages/foo/hooks/bar.py", tmp_path)

    output = _run_main(monkeypatch, capsys, payload)

    assert output == ""


def test_config_read_only_once_per_invocation(monkeypatch, capsys, tmp_path) -> None:
    """audit-flags.json is loaded exactly once per main() invocation, shared
    between evaluation_set_check_enabled() and resolve_state_path(). The
    evaluation-set-mapping.yaml lookup (Issue #237 / PR #243) reads base/local
    layers directly via _read_config_file (not load_package_config, since it
    needs a per-package merge rather than deep_merge()'s whole-list replace),
    so it must not appear here at all."""
    _make_package_dir(tmp_path, "quality-gates")
    _make_evaluation_doc(tmp_path, "quality-gates")

    call_counts: dict[str, int] = {}
    original_load_package_config = evaluation_set_checker.load_package_config

    def _counting_load_package_config(package_name, filename, project_dir):
        call_counts[filename] = call_counts.get(filename, 0) + 1
        return original_load_package_config(package_name, filename, project_dir)

    monkeypatch.setattr(
        evaluation_set_checker, "load_package_config", _counting_load_package_config
    )
    payload = _build_payload("packages/quality-gates/tests/test_foo.py", tmp_path)

    output = _run_main(monkeypatch, capsys, payload)

    assert output != ""
    assert call_counts == {"audit-flags.json": 1}


def test_mapping_config_files_each_read_once_per_invocation(monkeypatch, capsys, tmp_path) -> None:
    """The base and local evaluation-set-mapping.yaml layers are each read
    exactly once per main() invocation (PR #243 review follow-up: the
    per-package merge reads _read_config_file directly instead of going
    through load_package_config)."""
    _make_package_dir(tmp_path, "quality-gates")
    _make_evaluation_doc(tmp_path, "quality-gates")
    _write_mapping_config(
        tmp_path,
        [{"package": "quality-gates", "test_globs": ["packages/quality-gates/tests/*.py"]}],
    )

    call_count = {"n": 0}
    original_read_config_file = evaluation_set_checker._read_config_file

    def _counting_read_config_file(path):
        call_count["n"] += 1
        return original_read_config_file(path)

    monkeypatch.setattr(evaluation_set_checker, "_read_config_file", _counting_read_config_file)
    payload = _build_payload("packages/quality-gates/tests/test_foo.py", tmp_path)

    output = _run_main(monkeypatch, capsys, payload)

    assert output != ""
    assert call_count["n"] == 2  # base + local (local file need not exist)


# ---------------------------------------------------------------------------
# EV-10: main() の fail-open（例外捕捉 → stderr ログ + exit 0）
# ---------------------------------------------------------------------------


def test_main_fails_open_on_unexpected_exception(monkeypatch, capsys, tmp_path) -> None:
    payload = _build_payload("packages/quality-gates/tests/test_foo.py", tmp_path)
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))

    def _raise(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(evaluation_set_checker, "identify_package", _raise)

    with pytest.raises(SystemExit) as exc_info:
        evaluation_set_checker.main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "boom" in captured.err
