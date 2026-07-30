import json
import sys
from io import StringIO

import pytest

from tests.module_loader import load_module

post_test_analysis = load_module(
    "post_test_analysis", "packages/quality-gates/hooks/post-test-analysis.py"
)

# post_test_analysis's `from quality_gate_config import ...` (triggered by load_module
# above) registers the real shared module under its natural name "quality_gate_config"
# in sys.modules. Reuse that cached module to assert there is no locally-duplicated
# default state dict drifting from the one shared with test-gate-checker.py.
quality_gate_config = sys.modules["quality_gate_config"]


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
    """Provide an isolated real project dir and bypass real git lookups.

    project_dir must be a real, writable directory (not a fake path string)
    because the state file now resolves to
    <project_dir>/.claude/state/test-gate-checker.json instead of a single
    overridable /tmp constant.
    """
    monkeypatch.setattr(
        post_test_analysis, "get_project_state_key", lambda project_dir: project_dir
    )
    yield str(tmp_path)


def test_record_test_result_resets_on_pass(_clean_state) -> None:
    """Successful test run should reset counters and warned flag."""
    project_dir = _clean_state

    # Set up pre-existing state with modifications
    state = {
        "files_modified_since_test": ["src/auth.py", "src/models.py"],
        "lines_modified_since_test": 85,
        "last_test_result": None,
        "warned": True,
    }
    post_test_analysis.save_test_gate_state(project_dir, state)

    # Record a passing test
    post_test_analysis.record_test_result("pytest", True, project_dir)

    reloaded = post_test_analysis.load_test_gate_state(project_dir)
    assert reloaded["files_modified_since_test"] == []
    assert reloaded["lines_modified_since_test"] == 0
    assert reloaded["warned"] is False
    assert reloaded["last_test_result"]["passed"] is True
    assert reloaded["last_test_result"]["command"] == "pytest"


# ---------------------------------------------------------------------------
# DEFAULT_TEST_GATE_STATE de-duplication (shared with test-gate-checker.py)
# ---------------------------------------------------------------------------


def test_uses_shared_default_test_gate_state_constant() -> None:
    """post-test-analysis.py must not define its own default state dict."""
    assert not hasattr(post_test_analysis, "_DEFAULT_TEST_GATE_STATE")
    assert post_test_analysis.DEFAULT_TEST_GATE_STATE is quality_gate_config.DEFAULT_TEST_GATE_STATE


def test_load_test_gate_state_honors_shared_default(_clean_state, monkeypatch) -> None:
    """load_test_gate_state must fall back to quality_gate_config's shared default."""
    project_dir = _clean_state

    sentinel_default = {"files_modified_since_test": [], "sentinel": True}
    monkeypatch.setattr(post_test_analysis, "DEFAULT_TEST_GATE_STATE", sentinel_default)

    state = post_test_analysis.load_test_gate_state(project_dir)
    assert state == sentinel_default


def test_record_test_result_preserves_on_fail(_clean_state) -> None:
    """Failed test run should keep counters (changes not validated)."""
    project_dir = _clean_state

    state = {
        "files_modified_since_test": ["src/auth.py", "src/models.py"],
        "lines_modified_since_test": 85,
        "last_test_result": None,
        "warned": True,
    }
    post_test_analysis.save_test_gate_state(project_dir, state)

    # Record a failing test
    post_test_analysis.record_test_result("pytest", False, project_dir)

    reloaded = post_test_analysis.load_test_gate_state(project_dir)
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
        post_test_analysis,
        "load_quality_gate_config",
        # block_on_failed_test を明示的に無効化し、このテストの焦点である audit
        # イベント記録（payload の内容）と block_on_failed_test の既定値検証
        # （EV-11/12/19 は専用テストで別途検証する）を分離する。
        lambda _project_dir, config=None: {"enabled": True, "block_on_failed_test": False},
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
        lambda _project_dir, config=None: {"enabled": True, "block_on_failed_test": True},
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
        lambda _project_dir, config=None: {"enabled": True, "block_on_failed_test": True},
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


def test_emit_quality_gate_event_treats_missing_enabled_key_as_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config に `enabled` キーが無い場合でも quality_gate 判定を続行する（対称なデフォルト）。

    test_test_gate_checker.py の test_enabled_defaults_to_true_when_key_missing と対になる
    テスト。post-test-analysis.py 側の判定も同じデフォルト（True）に揃っていることを保証する。
    """
    captured: dict = {}

    monkeypatch.setattr(
        post_test_analysis, "resolve_project_root_from_hook_data", lambda data: data["cwd"]
    )
    monkeypatch.setattr(
        post_test_analysis,
        "load_quality_gate_config",
        lambda _project_dir, config=None: {},  # `enabled` キーが存在しない
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
        exit_code=1,
        gate_passed=False,
        output="FAILED test_example.py::test_case",
        detected_by="exit_code",
    )

    # enabled キー欠落時はデフォルト True として扱われ、イベントが記録される
    # （enabled=False によるショートサーキットで記録がスキップされない）。
    # block_on_failed_test も同様にキー欠落時は既定 True（2026-07-03 裁定、
    # EV-11/12/19）のためブロックする。
    assert blocking is True
    assert captured["type"] == "quality_gate"
    assert captured["payload"]["passed"] is False


def test_test_gate_checker_and_post_test_analysis_interoperate_on_shared_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """test-gate-checker.py と post-test-analysis.py が同じ共有状態ファイル・キー形式で連携する。

    修正1のスコープ化後も、片方が書いた project-scoped 状態をもう片方が正しく
    読み書きできる（ゲート連携が壊れていない）ことを保証する。
    """
    test_gate_checker = load_module(
        "test_gate_checker_interop", "packages/quality-gates/hooks/test-gate-checker.py"
    )

    # Real, writable directory: the shared state file now resolves to
    # <project_dir>/.claude/state/test-gate-checker.json (same STATE_FILENAME
    # in both modules) instead of a single overridable /tmp constant.
    project_dir = str(tmp_path / "interop-project")

    for module in (test_gate_checker, post_test_analysis):
        monkeypatch.setattr(module, "get_project_state_key", lambda p: p)

    # test-gate-checker.py が編集を蓄積する。
    state = test_gate_checker.load_test_gate_state(project_dir)
    state["files_modified_since_test"] = ["a.py", "b.py", "c.py"]
    state["lines_modified_since_test"] = 150
    state["warned"] = True
    test_gate_checker.save_test_gate_state(project_dir, state)

    # post-test-analysis.py がテスト成功を記録し、カウンタをリセットする。
    post_test_analysis.record_test_result("pytest -q", True, project_dir)

    # test-gate-checker.py 側から見てもリセットが反映されている。
    reloaded = test_gate_checker.load_test_gate_state(project_dir)
    assert reloaded["files_modified_since_test"] == []
    assert reloaded["lines_modified_since_test"] == 0
    assert reloaded["warned"] is False
    assert reloaded["last_test_result"]["passed"] is True


# ---------------------------------------------------------------------------
# EV-11/12/19: block_on_failed_test の既定値（opt-out 方式）
# ---------------------------------------------------------------------------


def test_emit_quality_gate_event_blocks_by_default_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """block_on_failed_test キーが無い config では既定でブロックする（2026-07-03 裁定）。"""
    monkeypatch.setattr(
        post_test_analysis, "resolve_project_root_from_hook_data", lambda data: data["cwd"]
    )
    monkeypatch.setattr(
        post_test_analysis,
        "load_quality_gate_config",
        lambda _project_dir, config=None: {"enabled": True},  # block_on_failed_test 未設定
    )
    monkeypatch.setattr(post_test_analysis, "load_trace_state", lambda **_kwargs: {"tid": "tid-1"})
    monkeypatch.setattr(post_test_analysis, "emit_event", lambda *_args, **_kwargs: None)

    blocking = post_test_analysis.emit_quality_gate_event(
        {"session_id": "sid-1", "cwd": "/project"},
        command="pytest -q",
        exit_code=1,
        gate_passed=False,
        output="FAILED test_example.py::test_case",
    )

    assert blocking is True


def test_emit_quality_gate_event_opt_out_disables_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """block_on_failed_test=false を明示した場合のみブロックを解除する（opt-out）。"""
    monkeypatch.setattr(
        post_test_analysis, "resolve_project_root_from_hook_data", lambda data: data["cwd"]
    )
    monkeypatch.setattr(
        post_test_analysis,
        "load_quality_gate_config",
        lambda _project_dir, config=None: {"enabled": True, "block_on_failed_test": False},
    )
    monkeypatch.setattr(post_test_analysis, "load_trace_state", lambda **_kwargs: {"tid": "tid-1"})
    monkeypatch.setattr(post_test_analysis, "emit_event", lambda *_args, **_kwargs: None)

    blocking = post_test_analysis.emit_quality_gate_event(
        {"session_id": "sid-1", "cwd": "/project"},
        command="pytest -q",
        exit_code=1,
        gate_passed=False,
        output="FAILED test_example.py::test_case",
    )

    assert blocking is False


# ---------------------------------------------------------------------------
# main() end-to-end: EV-17 (stdin/stdout contract), EV-21 (enabled), EV-22 (masking)
# ---------------------------------------------------------------------------


def _make_stdin(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))


def test_main_blocks_by_default_when_config_lacks_block_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """config に block_on_failed_test キーが無い実運用相当でも既定でブロックする。"""
    monkeypatch.setattr(
        post_test_analysis,
        "load_package_config",
        lambda *_args: {"features": {"quality_gate": {"enabled": True}}},
    )
    _make_stdin(
        monkeypatch,
        {
            "tool_name": "Bash",
            "cwd": str(tmp_path),
            "tool_input": {"command": "pytest -q"},
            "tool_response": {"exit_code": 1, "stdout": "FAILED test_example.py::test_case"},
        },
    )

    with pytest.raises(SystemExit, match="2"):
        post_test_analysis.main()


def test_main_no_op_when_quality_gate_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """quality_gate.enabled=false のときは Codex 提案 additionalContext も出さない。

    EV-21 は「提案・警告・ブロック・audit イベント記録を含む全動作を行わない」
    ことを求めているため、stdout/exit code だけでなく、record_test_result が
    書き込む共有状態ファイル（.claude/state/test-gate-checker.json）が一切
    生成されないことも検証する（実装ギャップ: record_test_result が
    quality_gate.enabled チェックより前に呼ばれ、無効時でも状態書き込みが
    発生していた）。
    """
    monkeypatch.setattr(
        post_test_analysis,
        "load_package_config",
        lambda *_args: {"features": {"quality_gate": {"enabled": False}}},
    )
    _make_stdin(
        monkeypatch,
        {
            "tool_name": "Bash",
            "cwd": str(tmp_path),
            "tool_input": {"command": "pytest -q"},
            "tool_response": {"exit_code": 1, "stdout": "FAILED test_example.py::test_case"},
        },
    )

    state_file = tmp_path / ".claude" / "state" / "test-gate-checker.json"
    assert not state_file.exists()

    with pytest.raises(SystemExit, match="0"):
        post_test_analysis.main()

    assert capsys.readouterr().out == ""
    assert not state_file.exists()


def test_main_masks_secrets_in_debug_suggestion(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """additionalContext の Codex 提案は失敗出力中の秘匿情報をマスクしてから出す。"""
    monkeypatch.setattr(
        post_test_analysis,
        "load_package_config",
        lambda *args: (
            {"features": {"quality_gate": {"enabled": True, "block_on_failed_test": False}}}
            if args[0] == "audit"
            else {"codex": {"model": "gpt-test", "sandbox": {"analysis": "read-only"}}}
        ),
    )
    secret_line = "FAILED test_example.py::test_case api_key=sk-1234567890abcdefghijklmno"
    _make_stdin(
        monkeypatch,
        {
            "tool_name": "Bash",
            "cwd": str(tmp_path),
            "tool_input": {"command": "pytest -q"},
            "tool_response": {"exit_code": 1, "stdout": secret_line},
        },
    )

    with pytest.raises(SystemExit, match="0"):
        post_test_analysis.main()

    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "sk-1234567890abcdefghijklmno" not in context
    assert "[REDACTED]" in context


# ---------------------------------------------------------------------------
# EV-10: main() の fail-open（例外捕捉 → stderr ログ + exit 0）
# ---------------------------------------------------------------------------


def test_main_fails_open_on_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(post_test_analysis, "analyze", _raise)
    _make_stdin(
        monkeypatch,
        {
            "tool_name": "Bash",
            "cwd": str(tmp_path),
            "tool_input": {"command": "pytest -q"},
            "tool_response": {"exit_code": 1, "stdout": "FAILED test_example.py::test_case"},
        },
    )

    with pytest.raises(SystemExit, match="0"):
        post_test_analysis.main()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Hook error" in captured.err
    assert "boom" in captured.err
