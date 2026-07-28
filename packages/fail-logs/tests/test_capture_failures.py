"""capture-failures hook の副作用（JSONL 追記）と設定トグルを検証する。"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

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


def _patch_root_worktree(monkeypatch, root_dir: Path | None) -> None:
    """resolve_log_root が参照する共通 root 解決結果を差し替える。"""
    resolved = str(root_dir) if root_dir is not None else None
    monkeypatch.setitem(
        capture.resolve_log_root.__globals__,
        "resolve_root_worktree",
        lambda _project_dir: resolved,
    )


def _require_git() -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not available on PATH")


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )


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


def test_traversal_logs_dir_falls_back_to_default(monkeypatch, tmp_path) -> None:
    """logs_dir が project_dir 外を指す設定でも、デフォルトの場所へ安全に書き込む。"""
    project = _make_project(tmp_path)
    config_dir = project / ".claude" / "config" / "fail-logs"
    config_dir.mkdir(parents=True)
    (config_dir / "fail-logs.local.yaml").write_text("logs_dir: '../../../tmp/evil'\n")

    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-traversal",
            "tool_name": "Bash",
            "tool_input": {"command": "ls /nope"},
            "tool_response": {"exit_code": 2, "stdout": "no such file"},
        },
    )

    # デフォルトの場所（project 配下）に記録される
    records = _read_log(project)
    assert len(records) == 1
    assert records[0]["data"]["failure_type"] == "tool_error"

    # project_dir の外（tmp_path の外側）には failures.jsonl が作られない
    default_log_path = project / ".claude" / "logs" / "fail-logs" / "failures.jsonl"
    jsonl_files = [
        Path(root) / f
        for root, _dirs, files in os.walk(tmp_path)
        for f in files
        if f == "failures.jsonl"
    ]
    assert jsonl_files == [default_log_path]


# EV-21: worktree からの記録を root worktree に集約し、解決不能時は従来位置へ戻す。
def test_worktree_failure_is_written_to_root_log(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)

    _run_hook(
        monkeypatch,
        worktree,
        {
            "cwd": str(worktree),
            "session_id": "sess-root",
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "tool_response": {"exit_code": 1, "stdout": "failed"},
        },
    )

    assert len(_read_log(root)) == 1
    assert _read_log(worktree) == []


def test_root_resolution_failure_writes_to_project_log(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path / "project")
    _patch_root_worktree(monkeypatch, None)

    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-fallback",
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "tool_response": {"exit_code": 1, "stdout": "failed"},
        },
    )

    assert len(_read_log(project)) == 1


def test_legacy_worktree_log_is_migrated_once(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    legacy_path = worktree / ".claude" / "logs" / "fail-logs" / "failures.jsonl"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_content = json.dumps({"legacy": True}) + "\n"
    legacy_path.write_text(legacy_content, encoding="utf-8")
    _patch_root_worktree(monkeypatch, root)
    payload = {
        "cwd": str(worktree),
        "session_id": "sess-migrate-1",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "tool_response": {"exit_code": 1, "stdout": "failed"},
    }

    _run_hook(monkeypatch, worktree, payload)

    migrated_path = legacy_path.with_name(f"{legacy_path.name}.migrated")
    assert len(_read_log(root)) == 2
    assert not legacy_path.exists()
    assert migrated_path.read_text(encoding="utf-8") == legacy_content

    _run_hook(
        monkeypatch,
        worktree,
        {**payload, "session_id": "sess-migrate-2"},
    )

    root_records = _read_log(root)
    assert len(root_records) == 3
    assert sum(record.get("legacy") is True for record in root_records) == 1
    assert migrated_path.read_text(encoding="utf-8") == legacy_content


# EV-17: failure record は発生元ブランチを含み、git 障害でも記録を継続する。
def test_record_includes_originating_branch(monkeypatch, tmp_path, capsys) -> None:
    project = _make_project(tmp_path)
    monkeypatch.setattr(capture, "_resolve_branch", lambda _project_dir: "feat/example")

    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-branch",
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "tool_response": {"exit_code": 1, "stdout": "failed"},
        },
    )

    records = _read_log(project)
    assert len(records) == 1
    assert records[0]["data"]["branch"] == "feat/example"
    assert capsys.readouterr().out == ""


def test_unborn_head_returns_branch_name(tmp_path) -> None:
    _require_git()
    project = _make_project(tmp_path)
    _run_git(project, "init")

    assert capture._resolve_branch(str(project))


def test_masks_secrets_in_branch(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    raw_secret = "secret123456789012345"
    monkeypatch.setattr(
        capture,
        "_resolve_branch",
        lambda _project_dir: f"feature/api_key={raw_secret}",
    )

    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-secret-branch",
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "tool_response": {"exit_code": 1, "stdout": "failed"},
        },
    )

    records = _read_log(project)
    assert len(records) == 1
    assert "[REDACTED]" in records[0]["data"]["branch"]
    assert raw_secret not in json.dumps(records[0])


def test_missing_git_records_empty_branch(monkeypatch, tmp_path, capsys) -> None:
    project = _make_project(tmp_path)

    def _raise_file_not_found(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(capture.subprocess, "run", _raise_file_not_found)

    _run_hook(
        monkeypatch,
        project,
        {
            "cwd": str(project),
            "session_id": "sess-no-git",
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "tool_response": {"exit_code": 1, "stdout": "failed"},
        },
    )

    records = _read_log(project)
    assert len(records) == 1
    assert records[0]["data"]["branch"] == ""
    assert capsys.readouterr().out == ""


def test_unicode_decode_error_returns_empty_branch(monkeypatch, tmp_path) -> None:
    def _raise_unicode_decode_error(*_args: object, **_kwargs: object) -> None:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")

    monkeypatch.setattr(capture.subprocess, "run", _raise_unicode_decode_error)

    assert capture._resolve_branch(str(tmp_path)) == ""
