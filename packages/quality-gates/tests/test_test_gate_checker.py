import json
import os
import subprocess
import sys
from io import StringIO

import pytest

from tests.module_loader import REPO_ROOT, load_module

test_gate_checker = load_module(
    "test_gate_checker", "packages/quality-gates/hooks/test-gate-checker.py"
)

_HOOK_PATH = REPO_ROOT / "packages" / "quality-gates" / "hooks" / "test-gate-checker.py"

# test_gate_checker's `from quality_gate_config import ...` (triggered by load_module
# above) registers the real shared module under its natural name "quality_gate_config"
# in sys.modules (distinct from the "quality_gate_config_standalone" alias used by
# test_quality_gate_config.py). Reuse that cached module to assert there is no
# locally-duplicated default state dict drifting from the shared one.
quality_gate_config = sys.modules["quality_gate_config"]


# ---------------------------------------------------------------------------
# DEFAULT_TEST_GATE_STATE de-duplication (shared with post-test-analysis.py)
# ---------------------------------------------------------------------------


def test_uses_shared_default_test_gate_state_constant() -> None:
    """test-gate-checker.py must not define its own default state dict."""
    assert not hasattr(test_gate_checker, "_DEFAULT_TEST_GATE_STATE")
    assert test_gate_checker.DEFAULT_TEST_GATE_STATE is quality_gate_config.DEFAULT_TEST_GATE_STATE


def test_load_test_gate_state_honors_shared_default(tmp_path, monkeypatch) -> None:
    """load_test_gate_state must fall back to quality_gate_config's shared default."""
    monkeypatch.setattr(test_gate_checker, "get_project_state_key", lambda project_dir: project_dir)

    sentinel_default = {"files_modified_since_test": [], "sentinel": True}
    monkeypatch.setattr(test_gate_checker, "DEFAULT_TEST_GATE_STATE", sentinel_default)

    state = test_gate_checker.load_test_gate_state(str(tmp_path))
    assert state == sentinel_default


def test_load_test_gate_state_resolves_under_claude_state_dir(tmp_path, monkeypatch) -> None:
    """The state file must land under <project_dir>/.claude/state/, not /tmp."""
    monkeypatch.setattr(test_gate_checker, "get_project_state_key", lambda project_dir: project_dir)

    test_gate_checker.save_test_gate_state(
        str(tmp_path), {"files_modified_since_test": ["a.py"], "lines_modified_since_test": 1}
    )

    expected = tmp_path / ".claude" / "state" / test_gate_checker.STATE_FILENAME
    assert expected.is_file()


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


@pytest.fixture()
def _clean_state(tmp_path, monkeypatch):
    """Provide isolated real project dirs and bypass real git lookups.

    Project dirs must be real, writable directories (not fake path strings)
    because the state file now resolves to
    <project_dir>/.claude/state/test-gate-checker.json instead of a single
    overridable /tmp constant.
    """
    monkeypatch.setattr(test_gate_checker, "get_project_state_key", lambda project_dir: project_dir)
    project_a = str(tmp_path / "project-a")
    project_b = str(tmp_path / "project-b")
    yield project_a, project_b


def test_increments_file_count(_clean_state) -> None:
    project_a, _project_b = _clean_state
    state = test_gate_checker.load_test_gate_state(project_a)
    assert state["files_modified_since_test"] == []

    # Simulate adding a file
    state["files_modified_since_test"].append("src/auth.py")
    state["lines_modified_since_test"] += 20
    test_gate_checker.save_test_gate_state(project_a, state)

    reloaded = test_gate_checker.load_test_gate_state(project_a)
    assert reloaded["files_modified_since_test"] == ["src/auth.py"]
    assert reloaded["lines_modified_since_test"] == 20


def test_no_duplicate_files(_clean_state) -> None:
    project_a, _project_b = _clean_state
    state = test_gate_checker.load_test_gate_state(project_a)
    file_path = "src/auth.py"

    # Add same file twice (simulating two edits to same file)
    modified = state["files_modified_since_test"]
    if file_path not in modified:
        modified.append(file_path)
    if file_path not in modified:
        modified.append(file_path)

    assert modified.count(file_path) == 1


def test_warns_at_threshold(_clean_state) -> None:
    project_a, _project_b = _clean_state
    state = test_gate_checker.load_test_gate_state(project_a)
    state["files_modified_since_test"] = ["a.py", "b.py", "c.py"]
    state["lines_modified_since_test"] = 50
    state["warned"] = False
    test_gate_checker.save_test_gate_state(project_a, state)

    reloaded = test_gate_checker.load_test_gate_state(project_a)
    file_count = len(reloaded["files_modified_since_test"])
    file_threshold = test_gate_checker.DEFAULT_FILE_THRESHOLD

    # 3 files >= threshold of 3 → should warn
    assert file_count >= file_threshold
    assert not reloaded["warned"]


def test_warns_only_once(_clean_state) -> None:
    project_a, _project_b = _clean_state
    state = test_gate_checker.load_test_gate_state(project_a)
    state["files_modified_since_test"] = ["a.py", "b.py", "c.py", "d.py"]
    state["lines_modified_since_test"] = 200
    state["warned"] = True  # Already warned
    test_gate_checker.save_test_gate_state(project_a, state)

    reloaded = test_gate_checker.load_test_gate_state(project_a)
    # Even though thresholds exceeded, warned=True prevents re-warning
    assert reloaded["warned"] is True


def test_state_is_isolated_per_project(_clean_state) -> None:
    """Edits tracked for project A must not leak into project B's threshold judgement."""
    project_a, project_b = _clean_state
    state_a = test_gate_checker.load_test_gate_state(project_a)
    state_a["files_modified_since_test"] = ["a.py", "b.py", "c.py"]
    state_a["lines_modified_since_test"] = 500
    state_a["warned"] = True
    test_gate_checker.save_test_gate_state(project_a, state_a)

    state_b = test_gate_checker.load_test_gate_state(project_b)
    assert state_b["files_modified_since_test"] == []
    assert state_b["lines_modified_since_test"] == 0
    assert state_b["warned"] is False

    reloaded_a = test_gate_checker.load_test_gate_state(project_a)
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


# ---------------------------------------------------------------------------
# EV-10: main() の fail-open（例外捕捉 → stderr ログ + exit 0）
# ---------------------------------------------------------------------------


def test_main_fails_open_on_unexpected_exception(monkeypatch, tmp_path, capsys) -> None:

    payload = {
        "tool_name": "Write",
        "cwd": str(tmp_path),
        "tool_input": {"file_path": "src/main.py", "content": "print(1)\n"},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))

    def _raise(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(test_gate_checker, "load_package_config", _raise)

    with pytest.raises(SystemExit) as exc_info:
        test_gate_checker.main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Hook error" in captured.err
    assert "boom" in captured.err


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


def test_main_normalizes_subdirectory_before_disabled_config_lookup(
    monkeypatch, tmp_path, capsys
) -> None:
    """subdirectory cwd でも root の disabled 設定を使い、状態を変更しない。"""
    repo_root = tmp_path / "repo"
    (repo_root / ".claude").mkdir(parents=True)
    subdirectory = repo_root / "packages" / "sub"
    subdirectory.mkdir(parents=True)
    config_calls = []

    def _load_config(package_name: str, filename: str, project_dir: str) -> dict:
        config_calls.append((package_name, filename, project_dir))
        return {"features": {"quality_gate": {"enabled": False}}}

    def _fail_if_state_loaded(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        pytest.fail("state must not be loaded when quality_gate is disabled")

    payload = {
        "tool_name": "Write",
        "cwd": str(subdirectory),
        "tool_input": {"file_path": "src/main.py", "content": "print(1)\n"},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    monkeypatch.setattr(test_gate_checker, "load_package_config", _load_config)
    monkeypatch.setattr(test_gate_checker, "load_test_gate_state", _fail_if_state_loaded)

    with pytest.raises(SystemExit) as exc_info:
        test_gate_checker.main()

    assert exc_info.value.code == 0
    assert config_calls == [("audit", "audit-flags.json", str(repo_root))]
    assert capsys.readouterr().out == ""
    state_file = repo_root / ".claude" / "state" / test_gate_checker.STATE_FILENAME
    assert not state_file.exists()


# ---------------------------------------------------------------------------
# Issue #134 レビュー指摘: AI_ORCHESTRA_DIR 未設定時の hook_common フォールバック
# ---------------------------------------------------------------------------


def test_hook_runs_without_ai_orchestra_dir_env_var(tmp_path) -> None:
    """post-implementation-review.py と同じフォールバック欠落があったため、
    同じ回帰テストを適用する。AI_ORCHESTRA_DIR 未設定でも hook_common の
    import に失敗せず fail-open（exit 0）で終わることを確認する。"""
    payload = {
        "tool_name": "Write",
        "cwd": str(tmp_path),
        "tool_input": {"file_path": "src/module.py", "content": "line\n"},
    }
    env = {k: v for k, v in os.environ.items() if k != "AI_ORCHESTRA_DIR"}

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(_HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        timeout=30,
    )

    assert result.returncode == 0
    assert "ModuleNotFoundError" not in result.stderr
