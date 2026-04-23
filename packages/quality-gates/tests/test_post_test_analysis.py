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


def test_is_test_failure_true_when_exit_code_nonzero() -> None:
    assert post_test_analysis.is_test_failure(1, "all good")


def test_is_test_failure_true_when_output_contains_failure_indicator() -> None:
    assert post_test_analysis.is_test_failure(0, "2 tests FAILED")


def test_is_test_failure_false_for_successful_output() -> None:
    assert not post_test_analysis.is_test_failure(0, "12 passed")


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
        output="FAILED test_example.py::test_case",
    )

    assert blocking is False
    assert captured["type"] == "quality_gate"
    assert captured["payload"]["command"] == "pytest -q"
    assert captured["payload"]["exit_code"] == 1
    assert captured["payload"]["passed"] is False
    assert captured["payload"]["blocking"] is False
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
        output="FAILED",
    )

    assert blocking is True


def test_emit_quality_gate_event_uses_exit_code_for_success_even_with_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        command="pytest -q",
        exit_code=0,
        output="ValueError: expected output marker",
    )

    assert blocking is False
    assert captured["type"] == "quality_gate"
    assert captured["payload"]["passed"] is True
    assert captured["payload"]["blocking"] is False
