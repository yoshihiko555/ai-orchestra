import json

import pytest

from tests.module_loader import load_module

test_gate_checker = load_module(
    "test_gate_checker", "packages/quality-gates/hooks/test-gate-checker.py"
)


# ---------------------------------------------------------------------------
# is_code_file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "src/auth.py",
        "app/index.ts",
        "components/Button.tsx",
        "lib/utils.js",
        "main.go",
        "handler.rs",
        "Service.java",
        "app/page.jsx",
    ],
)
def test_is_code_file_returns_true_for_code_files(path: str) -> None:
    assert test_gate_checker.is_code_file(path)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "config.yaml",
        "data.json",
        "styles.css",
        "image.png",
        ".env",
    ],
)
def test_is_code_file_returns_false_for_non_code_files(path: str) -> None:
    assert not test_gate_checker.is_code_file(path)


# ---------------------------------------------------------------------------
# count_lines
# ---------------------------------------------------------------------------


def test_count_lines_ignores_empty_lines() -> None:
    content = "line1\n\nline2\n  \nline3\n"
    assert test_gate_checker.count_lines(content) == 3


# ---------------------------------------------------------------------------
# State management with tmp file
# ---------------------------------------------------------------------------

FAKE_PROJECT_A = "/fake/project-a"
FAKE_PROJECT_B = "/fake/project-b"


@pytest.fixture()
def _clean_state(tmp_path, monkeypatch):
    """Redirect state file to tmp_path and bypass real git lookups."""
    state_file = tmp_path / "test-gate-state.json"
    monkeypatch.setattr(test_gate_checker, "TEST_GATE_STATE_FILE", state_file)
    monkeypatch.setattr(test_gate_checker, "get_project_state_key", lambda project_dir: project_dir)
    yield state_file


def test_increments_file_count(_clean_state) -> None:
    state = test_gate_checker.load_test_gate_state(FAKE_PROJECT_A)
    assert state["files_modified_since_test"] == []

    # Simulate adding a file
    state["files_modified_since_test"].append("src/auth.py")
    state["lines_modified_since_test"] += 20
    test_gate_checker.save_test_gate_state(FAKE_PROJECT_A, state)

    reloaded = test_gate_checker.load_test_gate_state(FAKE_PROJECT_A)
    assert reloaded["files_modified_since_test"] == ["src/auth.py"]
    assert reloaded["lines_modified_since_test"] == 20


def test_no_duplicate_files(_clean_state) -> None:
    state = test_gate_checker.load_test_gate_state(FAKE_PROJECT_A)
    file_path = "src/auth.py"

    # Add same file twice (simulating two edits to same file)
    modified = state["files_modified_since_test"]
    if file_path not in modified:
        modified.append(file_path)
    if file_path not in modified:
        modified.append(file_path)

    assert modified.count(file_path) == 1


def test_warns_at_threshold(_clean_state) -> None:
    state = test_gate_checker.load_test_gate_state(FAKE_PROJECT_A)
    state["files_modified_since_test"] = ["a.py", "b.py", "c.py"]
    state["lines_modified_since_test"] = 50
    state["warned"] = False
    test_gate_checker.save_test_gate_state(FAKE_PROJECT_A, state)

    reloaded = test_gate_checker.load_test_gate_state(FAKE_PROJECT_A)
    file_count = len(reloaded["files_modified_since_test"])
    file_threshold = test_gate_checker.DEFAULT_FILE_THRESHOLD

    # 3 files >= threshold of 3 → should warn
    assert file_count >= file_threshold
    assert not reloaded["warned"]


def test_warns_only_once(_clean_state) -> None:
    state = test_gate_checker.load_test_gate_state(FAKE_PROJECT_A)
    state["files_modified_since_test"] = ["a.py", "b.py", "c.py", "d.py"]
    state["lines_modified_since_test"] = 200
    state["warned"] = True  # Already warned
    test_gate_checker.save_test_gate_state(FAKE_PROJECT_A, state)

    reloaded = test_gate_checker.load_test_gate_state(FAKE_PROJECT_A)
    # Even though thresholds exceeded, warned=True prevents re-warning
    assert reloaded["warned"] is True


def test_state_is_isolated_per_project(_clean_state) -> None:
    """Edits tracked for project A must not leak into project B's threshold judgement."""
    state_a = test_gate_checker.load_test_gate_state(FAKE_PROJECT_A)
    state_a["files_modified_since_test"] = ["a.py", "b.py", "c.py"]
    state_a["lines_modified_since_test"] = 500
    state_a["warned"] = True
    test_gate_checker.save_test_gate_state(FAKE_PROJECT_A, state_a)

    state_b = test_gate_checker.load_test_gate_state(FAKE_PROJECT_B)
    assert state_b["files_modified_since_test"] == []
    assert state_b["lines_modified_since_test"] == 0
    assert state_b["warned"] is False

    reloaded_a = test_gate_checker.load_test_gate_state(FAKE_PROJECT_A)
    assert reloaded_a["files_modified_since_test"] == ["a.py", "b.py", "c.py"]
    assert reloaded_a["lines_modified_since_test"] == 500


# ---------------------------------------------------------------------------
# build_warning_message
# ---------------------------------------------------------------------------


def test_build_warning_message_no_test_history() -> None:
    msg = test_gate_checker.build_warning_message(4, 150, has_test_history=False)
    assert "[Test Gate]" in msg
    assert "4 files modified" in msg
    assert "~150 lines changed" in msg
    assert "No tests have been run in this session" in msg


def test_build_warning_message_with_test_history() -> None:
    msg = test_gate_checker.build_warning_message(3, 80, has_test_history=True)
    assert "No tests have been run since last changes" in msg


# ---------------------------------------------------------------------------
# is_quality_gate_enabled (config loading)
# ---------------------------------------------------------------------------


def test_respects_enabled_flag(tmp_path, monkeypatch) -> None:
    """When enabled=false in config, quality gate should be disabled."""
    config_dir = tmp_path / ".claude" / "config" / "audit"
    config_dir.mkdir(parents=True)
    config = {
        "features": {
            "quality_gate": {
                "enabled": False,
                "test_file_threshold": 3,
                "test_line_threshold": 100,
            }
        }
    }
    with open(config_dir / "audit-flags.json", "w") as f:
        json.dump(config, f)

    assert not test_gate_checker.is_quality_gate_enabled(str(tmp_path))


def test_enabled_when_flag_true(tmp_path) -> None:
    """When enabled=true in config, quality gate should be enabled."""
    config_dir = tmp_path / ".claude" / "config" / "audit"
    config_dir.mkdir(parents=True)
    config = {
        "features": {
            "quality_gate": {
                "enabled": True,
                "test_file_threshold": 3,
                "test_line_threshold": 100,
            }
        }
    }
    with open(config_dir / "audit-flags.json", "w") as f:
        json.dump(config, f)

    assert test_gate_checker.is_quality_gate_enabled(str(tmp_path))


def test_enabled_defaults_to_true_when_key_missing(tmp_path) -> None:
    """When quality_gate config exists but lacks an `enabled` key, default to True.

    This keeps the default symmetric with post-test-analysis.py's blocking check,
    matching audit-flags.json's base value (enabled: true).
    """
    config_dir = tmp_path / ".claude" / "config" / "audit"
    config_dir.mkdir(parents=True)
    config = {
        "features": {
            "quality_gate": {
                "test_file_threshold": 3,
                "test_line_threshold": 100,
            }
        }
    }
    with open(config_dir / "audit-flags.json", "w") as f:
        json.dump(config, f)

    assert test_gate_checker.is_quality_gate_enabled(str(tmp_path))
