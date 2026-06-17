import sys

import pytest

from tests.module_loader import load_module

post_test_analysis = load_module(
    "post_test_analysis", "packages/quality-gates/hooks/post-test-analysis.py"
)


def test_module_loads_without_ai_orchestra_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)

    saved_hook_common = sys.modules.pop("hook_common", None)
    saved_event_logger = sys.modules.pop("event_logger", None)
    try:
        module = load_module(
            "post_test_analysis_without_orchestra",
            "packages/quality-gates/hooks/post-test-analysis.py",
        )
    finally:
        if saved_hook_common is not None:
            sys.modules["hook_common"] = saved_hook_common
        if saved_event_logger is not None:
            sys.modules["event_logger"] = saved_event_logger

    assert callable(module.load_quality_gate_config)


@pytest.mark.parametrize(
    "command",
    [
        "pytest",
        "npm test",
        "npm run test",
        "uv run pytest tests/",
        "cargo test -q",
        "ruff check .",
        "mypy src/",
    ],
)
def test_is_test_command_detects_supported_commands(command: str) -> None:
    assert post_test_analysis.is_test_command(command)


@pytest.mark.parametrize("command", ["ls -la", "npm run build", "ruff format ."])
def test_is_test_command_ignores_non_test_commands(command: str) -> None:
    assert not post_test_analysis.is_test_command(command)


def test_analyze_detects_failure_on_nonzero_exit() -> None:
    """終了コードが非ゼロなら失敗（最も信頼できる根拠）。"""
    failure = post_test_analysis.analyze(
        "Bash",
        {"command": "pytest -q"},
        {"exit_code": 1, "stdout": "1 failed in 0.12s"},
    )
    assert failure is not None
    assert failure["detected_by"] == "exit_code"


def test_analyze_detects_failure_on_output_marker_when_exit_code_masked() -> None:
    """パイプで exit code がマスクされても出力パターンで失敗を検知する。"""
    failure = post_test_analysis.analyze(
        "Bash",
        {"command": "pytest -q | tail -30"},
        {"exit_code": 0, "stdout": "FAILED tests/test_x.py::test_y\n1 failed in 0.30s"},
    )
    assert failure is not None
    assert failure["detected_by"] == "output_pattern"


def test_analyze_returns_none_for_successful_output() -> None:
    """実 pytest の成功出力では失敗としない。"""
    failure = post_test_analysis.analyze(
        "Bash",
        {"command": "pytest -q"},
        {"exit_code": 0, "stdout": "12 passed in 0.45s"},
    )
    assert failure is None


def test_extract_failure_summary_returns_top_3_lines() -> None:
    output = "\n".join(
        [
            "setup line",
            "FAILED tests/test_a.py::test_x",
            "AssertionError: expected 1 got 2",
            "TypeError: bad operand",
            "FAILED tests/test_b.py::test_y",
        ]
    )

    summary = post_test_analysis.extract_failure_summary(output)
    lines = summary.split("\n")

    assert len(lines) == 3
    assert lines[0] == "FAILED tests/test_a.py::test_x"
    assert lines[1] == "AssertionError: expected 1 got 2"
    assert lines[2] == "TypeError: bad operand"


def test_extract_failure_summary_returns_default_when_no_match() -> None:
    assert post_test_analysis.extract_failure_summary("all passed") == "Test failure detected"


# ---------------------------------------------------------------------------
# record_test_result (shared state management)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _clean_state(tmp_path, monkeypatch):
    """Redirect state file to tmp_path so tests don't interfere."""
    state_file = tmp_path / "test-gate-state.json"
    monkeypatch.setattr(post_test_analysis, "TEST_GATE_STATE_FILE", state_file)
    yield state_file


def test_record_test_result_resets_on_pass(_clean_state) -> None:
    """Successful test run should reset counters and warned flag."""
    # Set up pre-existing state with modifications
    state = {
        "files_modified_since_test": ["src/auth.py", "src/models.py"],
        "lines_modified_since_test": 85,
        "last_test_result": None,
        "warned": True,
    }
    post_test_analysis.save_test_gate_state(state)

    # Record a passing test
    post_test_analysis.record_test_result("pytest", passed=True)

    reloaded = post_test_analysis.load_test_gate_state()
    assert reloaded["files_modified_since_test"] == []
    assert reloaded["lines_modified_since_test"] == 0
    assert reloaded["warned"] is False
    assert reloaded["last_test_result"]["passed"] is True
    assert reloaded["last_test_result"]["command"] == "pytest"


def test_record_test_result_preserves_on_fail(_clean_state) -> None:
    """Failed test run should keep counters (changes not validated)."""
    state = {
        "files_modified_since_test": ["src/auth.py", "src/models.py"],
        "lines_modified_since_test": 85,
        "last_test_result": None,
        "warned": True,
    }
    post_test_analysis.save_test_gate_state(state)

    # Record a failing test
    post_test_analysis.record_test_result("pytest", passed=False)

    reloaded = post_test_analysis.load_test_gate_state()
    assert reloaded["files_modified_since_test"] == ["src/auth.py", "src/models.py"]
    assert reloaded["lines_modified_since_test"] == 85
    assert reloaded["warned"] is True
    assert reloaded["last_test_result"]["passed"] is False


def test_emit_quality_gate_event_records_audit_event(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    monkeypatch.setattr(
        post_test_analysis, "resolve_project_root_from_hook_data", lambda data: data["cwd"]
    )
    monkeypatch.setattr(
        post_test_analysis, "load_quality_gate_config", lambda _project_dir: {"enabled": True}
    )
    monkeypatch.setattr(
        post_test_analysis, "load_trace_state", lambda **_kwargs: {"tid": "tid-123"}
    )
    monkeypatch.setattr(
        post_test_analysis,
        "emit_event",
        lambda event_type, payload, **kwargs: captured.update(
            {"type": event_type, "payload": payload, "kwargs": kwargs}
        ),
    )

    blocking = post_test_analysis.emit_quality_gate_event(
        {
            "session_id": "sid-1",
            "cwd": "/project",
        },
        command="pytest -q",
        exit_code=1,
        gate_passed=False,
        output="FAILED test_example.py::test_case",
        detected_by="exit_code",
    )

    assert blocking is False
    assert captured["type"] == "quality_gate"
    assert captured["payload"]["command"] == "pytest -q"
    assert captured["payload"]["exit_code"] == 1
    assert captured["payload"]["passed"] is False
    assert captured["payload"]["blocking"] is False
    assert captured["payload"]["detected_by"] == "exit_code"
    assert captured["kwargs"]["session_id"] == "sid-1"
    assert captured["kwargs"]["tid"] == "tid-123"


def test_emit_quality_gate_event_returns_blocking_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        post_test_analysis, "resolve_project_root_from_hook_data", lambda data: data["cwd"]
    )
    monkeypatch.setattr(
        post_test_analysis,
        "load_quality_gate_config",
        lambda _project_dir: {"enabled": True, "block_on_failed_test": True},
    )
    monkeypatch.setattr(
        post_test_analysis, "load_trace_state", lambda **_kwargs: {"tid": "tid-123"}
    )
    monkeypatch.setattr(post_test_analysis, "emit_event", lambda *_args, **_kwargs: None)

    blocking = post_test_analysis.emit_quality_gate_event(
        {"session_id": "sid-1", "cwd": "/project"},
        command="pytest -q",
        exit_code=1,
        gate_passed=False,
        output="FAILED",
    )

    assert blocking is True


def test_emit_quality_gate_event_records_failure_when_exit_code_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """パイプマスク回帰: exit_code=0 でも gate_passed=False なら failed 記録 + ブロック。

    `pytest ... | tail -30` のようにパイプで終了コードがマスクされた失敗を、
    failure_detector が出力パターンで検知し gate_passed=False を導出する。
    その結果が payload に passed:false / blocking:true として記録されることを保証する。
    """
    captured: dict = {}

    monkeypatch.setattr(
        post_test_analysis, "resolve_project_root_from_hook_data", lambda data: data["cwd"]
    )
    monkeypatch.setattr(
        post_test_analysis,
        "load_quality_gate_config",
        lambda _project_dir: {"enabled": True, "block_on_failed_test": True},
    )
    monkeypatch.setattr(
        post_test_analysis, "load_trace_state", lambda **_kwargs: {"tid": "tid-123"}
    )
    monkeypatch.setattr(
        post_test_analysis,
        "emit_event",
        lambda event_type, payload, **kwargs: captured.update(
            {"type": event_type, "payload": payload, "kwargs": kwargs}
        ),
    )

    blocking = post_test_analysis.emit_quality_gate_event(
        {"session_id": "sid-1", "cwd": "/project"},
        command="pytest -q | tail -30",
        exit_code=0,
        gate_passed=False,
        output="FAILED tests/test_x.py::test_y\n1 failed in 0.30s",
        detected_by="output_pattern",
    )

    assert blocking is True
    assert captured["type"] == "quality_gate"
    assert captured["payload"]["exit_code"] == 0
    assert captured["payload"]["passed"] is False
    assert captured["payload"]["blocking"] is True
    assert captured["payload"]["detected_by"] == "output_pattern"


def test_pipe_masked_failure_flow_derives_failed_gate_and_fires_suggestion() -> None:
    """パイプマスク回帰（フロー）: exit_code=0 + "1 failed" → gate_passed=False。

    main() が `analyze` から導出する gate_passed / analysis_failed を直接検証する。
    analysis_failed=True は Codex debug suggestion 発火条件（非ブロック時）に一致する。
    """
    tool_input = {"command": "pytest -q | tail -30"}
    tool_response = {"exit_code": 0, "stdout": "FAILED tests/test_x.py::test_y\n1 failed in 0.3s"}

    failure = post_test_analysis.analyze("Bash", tool_input, tool_response)
    gate_passed = failure is None
    analysis_failed = not gate_passed

    assert gate_passed is False
    assert analysis_failed is True
    assert failure["detected_by"] == "output_pattern"
