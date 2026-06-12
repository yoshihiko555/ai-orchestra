"""capture-failures hook の副作用（JSONL 追記）と設定トグルを検証する。"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

from tests.module_loader import load_module

capture = load_module("capture_failures", "packages/fail-logs/hooks/capture-failures.py")


def _make_project(tmp_path: Path) -> Path:
    """`.claude` を持つプロジェクトルートを用意する。"""
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _run_hook(monkeypatch, project_dir: Path, payload: dict) -> None:
    """stdin をモックして hook の main() を実行する。"""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    capture.main()


def _read_log(project_dir: Path) -> list[dict]:
    log_path = project_dir / ".claude" / "logs" / "fail-logs" / "failures.jsonl"
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def test_records_bash_nonzero_failure(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-1",
            "tool_name": "Bash",
            "tool_input": {"command": "ls /nope"},
            "tool_response": {"exit_code": 2, "stdout": "no such file"},
        },
    )
    records = _read_log(project)
    assert len(records) == 1
    rec = records[0]
    assert rec["v"] == 1
    assert rec["type"] == "failure"
    assert rec["sid"] == "sess-1"
    assert rec["data"]["failure_type"] == "tool_error"
    assert rec["data"]["detected_by"] == "exit_code"
    assert rec["data"]["tool"] == "Bash"


def test_records_pipe_masked_test_failure(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-2",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/ | tail -5"},
            "tool_response": {"exit_code": 0, "stdout": "FAILED tests/test_a.py\n1 failed"},
        },
    )
    records = _read_log(project)
    assert len(records) == 1
    assert records[0]["data"]["failure_type"] == "test_failure"
    assert records[0]["data"]["detected_by"] == "output_pattern"


def test_does_not_record_success(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-3",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
            "tool_response": {"exit_code": 0, "stdout": "4 passed"},
        },
    )
    assert _read_log(project) == []


def test_disabled_via_config(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    config_dir = project / ".claude" / "config" / "fail-logs"
    config_dir.mkdir(parents=True)
    (config_dir / "fail-logs.local.yaml").write_text("enabled: false\n")

    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-4",
            "tool_name": "Bash",
            "tool_input": {"command": "ls /nope"},
            "tool_response": {"exit_code": 2, "stdout": "boom"},
        },
    )
    assert _read_log(project) == []


def test_target_toggle_skips_test_failure(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    config_dir = project / ".claude" / "config" / "fail-logs"
    config_dir.mkdir(parents=True)
    (config_dir / "fail-logs.local.yaml").write_text("targets:\n  test_failure: false\n")

    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-5",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
            "tool_response": {"exit_code": 1, "stdout": "1 failed"},
        },
    )
    assert _read_log(project) == []


def test_masks_secrets_in_excerpt(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-6",
            "tool_name": "Bash",
            "tool_input": {"command": "deploy --token=sk-abcdefghijklmnopqrstuvwxyz123456"},
            "tool_response": {
                "exit_code": 1,
                "stdout": "auth failed sk-abcdefghijklmnopqrstuvwxyz123456",
            },
        },
    )
    records = _read_log(project)
    assert len(records) == 1
    blob = json.dumps(records[0])
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in blob
    assert "[REDACTED]" in blob


def test_records_non_bash_tool_error(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-7",
            "tool_name": "Edit",
            "tool_input": {"file_path": "x.py"},
            "tool_response": {"error": "String to replace not found"},
        },
    )
    records = _read_log(project)
    assert len(records) == 1
    assert records[0]["data"]["failure_type"] == "tool_error"
    assert records[0]["data"]["tool"] == "Edit"


def test_log_file_permission_is_owner_only(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-8",
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "tool_response": {"exit_code": 1, "stdout": ""},
        },
    )
    log_path = project / ".claude" / "logs" / "fail-logs" / "failures.jsonl"
    assert log_path.exists()
    mode = os.stat(log_path).st_mode & 0o777
    assert mode == 0o600


def test_masks_azure_sas_token(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-azure",
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "tool_response": {"exit_code": 1, "stdout": "SharedAccessSignature=abc123secret"},
        },
    )
    blob = json.dumps(_read_log(project)[0])
    assert "abc123secret" not in blob
    assert "[REDACTED]" in blob


def test_invalid_max_excerpt_chars_falls_back(monkeypatch, tmp_path) -> None:
    # 型不正な config 値でもクラッシュせず（サイレントドロップせず）記録する
    project = _make_project(tmp_path)
    config_dir = project / ".claude" / "config" / "fail-logs"
    config_dir.mkdir(parents=True)
    (config_dir / "fail-logs.local.yaml").write_text("max_excerpt_chars: not-a-number\n")

    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-bad-cfg",
            "tool_name": "Bash",
            "tool_input": {"command": "ls /nope"},
            "tool_response": {"exit_code": 2, "stdout": "no such file"},
        },
    )
    records = _read_log(project)
    assert len(records) == 1
    assert records[0]["data"]["failure_type"] == "tool_error"


def test_empty_input_is_noop(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    capture.main()
    assert _read_log(project) == []
