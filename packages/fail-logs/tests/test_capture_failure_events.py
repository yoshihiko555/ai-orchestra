"""capture-failures hook の新しいイベント（PostToolUseFailure / PermissionDenied）を検証する。

評価セット docs/evaluation/fail-logs.md の EV-11・EV-12・EV-17・EV-22〜EV-30 に対応する。
入力の形は Claude Code 2.1.317 の実機で確かめたもの（設計 docs/design/fail-logs.md §3(f)・§15）。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

capture = load_module("capture_failures_events", "packages/fail-logs/hooks/capture-failures.py")

CAPTURE_SCRIPT = REPO_ROOT / "packages" / "fail-logs" / "hooks" / "capture-failures.py"
LEGACY_DATA_KEYS = {
    "failure_type",
    "error_type",
    "detected_by",
    "command_kind",
    "tool",
    "command",
    "error_excerpt",
    "exit_code",
    "cwd",
    "branch",
}
# 強化版マスキングの評価に使う見本と同じ形（audit の test_secret_masking.py）。
API_KEY_SAMPLE = "sk-" + "abcdefghijklmnopqrstuvwxyz012345"
GITHUB_TOKEN_SAMPLE = "gh" + "p_" + "A" * 36


@pytest.fixture(autouse=True)
def _use_repo_masking(monkeypatch) -> None:
    """強化版マスキングをこの checkout の audit から読む（シェルの AI_ORCHESTRA_DIR に依存しない）。"""
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))


def _make_project(tmp_path: Path, local_config: str | None = None) -> Path:
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    if local_config is not None:
        config_dir = tmp_path / ".claude" / "config" / "fail-logs"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "fail-logs.local.yaml").write_text(local_config, encoding="utf-8")
    return tmp_path


def _run_hook(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    capture.main()


def _read_log(project_dir: Path) -> list[dict]:
    log_path = project_dir / ".claude" / "logs" / "fail-logs" / "failures.jsonl"
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def _failure_payload(project: Path, **overrides: object) -> dict:
    payload: dict = {
        "cwd": str(project),
        "session_id": "sess-failure",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "make build", "description": "build"},
        "tool_use_id": "toolu_01ABC",
        "error": "Exit code 3\nout\nerr",
        "is_interrupt": False,
        "duration_ms": 12,
    }
    payload.update(overrides)
    return payload


def _denied_payload(project: Path, **overrides: object) -> dict:
    payload: dict = {
        "cwd": str(project),
        "session_id": "sess-denied",
        "hook_event_name": "PermissionDenied",
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force origin main"},
        "tool_use_id": "toolu_01DEN",
        "reason": "The server-side auto mode classifier judged this action dangerous",
    }
    payload.update(overrides)
    return payload


# EV-22: Bash の `Exit code N` から終了コード・検知根拠・抜粋（2 行目以降）を記録する。
def test_bash_exit_code_failure_is_recorded(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(monkeypatch, _failure_payload(project))

    records = _read_log(project)
    assert len(records) == 1
    data = records[0]["data"]
    assert data["failure_type"] == "tool_error"
    assert data["detected_by"] == "exit_code"
    assert data["exit_code"] == 3
    assert data["command_kind"] == "shell"
    assert data["command"] == "make build"
    assert data["error_excerpt"] == "out\nerr"
    assert data["tool"] == "Bash"


# EV-22: failure_type はコマンドの種別で test / lint / cli、それ以外は tool_error になる。
@pytest.mark.parametrize(
    ("command", "failure_type", "command_kind"),
    [
        ("uv run pytest -q tests", "test_failure", "test"),
        ("ruff check .", "lint_failure", "lint"),
        ("codex exec --model m 'q'", "cli_failure", "cli"),
        ("ls /nope", "tool_error", "shell"),
    ],
)
def test_failure_type_follows_command_kind(
    monkeypatch, tmp_path, command: str, failure_type: str, command_kind: str
) -> None:
    project = _make_project(tmp_path)
    _run_hook(
        monkeypatch,
        _failure_payload(project, tool_input={"command": command}, error="Exit code 1\nboom"),
    )

    data = _read_log(project)[0]["data"]
    assert data["failure_type"] == failure_type
    assert data["command_kind"] == command_kind
    assert data["exit_code"] == 1


# EV-23: `Exit code N` で始まらない Bash のエラーは tool_error / tool_error / exit_code null。
def test_bash_error_without_exit_code_line(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    error = "Sandbox is enabled but failed to initialize: EPERM: operation not permitted"
    _run_hook(monkeypatch, _failure_payload(project, error=error))

    data = _read_log(project)[0]["data"]
    assert data["failure_type"] == "tool_error"
    assert data["detected_by"] == "tool_error"
    assert data["exit_code"] is None
    assert data["command"] == "make build"
    assert data["command_kind"] == "shell"
    assert data["error_excerpt"] == error


# EV-23: Bash 以外のツールの失敗は command が空・command_kind が none になる。
def test_non_bash_tool_failure(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(
        monkeypatch,
        _failure_payload(
            project,
            tool_name="Read",
            tool_input={"file_path": "/tmp/nope.txt"},
            error="File does not exist.",
        ),
    )

    data = _read_log(project)[0]["data"]
    assert data["failure_type"] == "tool_error"
    assert data["detected_by"] == "tool_error"
    assert data["tool"] == "Read"
    assert data["command"] == ""
    assert data["command_kind"] == "none"
    assert data["exit_code"] is None


# EV-24: 中断（is_interrupt: true）は記録しない。
def test_interrupt_is_not_recorded(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(monkeypatch, _failure_payload(project, is_interrupt=True))

    assert _read_log(project) == []


# EV-25: auto mode の拒否を permission_denied として記録し、抜粋は reason から作る。
def test_permission_denied_is_recorded(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(monkeypatch, _denied_payload(project))

    records = _read_log(project)
    assert len(records) == 1
    data = records[0]["data"]
    assert data["failure_type"] == "permission_denied"
    assert data["detected_by"] == "permission"
    assert data["error_type"] == "permission"
    assert data["exit_code"] is None
    assert data["command"] == "git push --force origin main"
    assert data["command_kind"] == "shell"
    assert data["error_excerpt"].startswith("The server-side auto mode classifier")
    assert data["tool_use_id"] == "toolu_01DEN"


# EV-25: Bash 以外のツールの拒否は command が空になる。
def test_permission_denied_non_bash_tool(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(
        monkeypatch,
        _denied_payload(project, tool_name="WebFetch", tool_input={"url": "https://example.com"}),
    )

    data = _read_log(project)[0]["data"]
    assert data["command"] == ""
    assert data["command_kind"] == "none"
    assert data["tool"] == "WebFetch"


# EV-26: 1 回の失敗は 1 件だけ記録し、出力パターンの検知を重ねて適用しない。
def test_failure_event_is_recorded_once_without_output_pattern(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(
        monkeypatch,
        _failure_payload(
            project,
            tool_input={"command": "pytest -q"},
            error="Exit code 1\nFAILED tests/test_a.py::test_x\n1 failed",
            tool_response={"stdout": "1 failed", "exit_code": 0},
        ),
    )

    records = _read_log(project)
    assert len(records) == 1
    assert records[0]["data"]["detected_by"] == "exit_code"


# EV-16: 新しいイベントの記録も重複排除しない（同じ入力 2 回で 2 件）。
def test_new_event_duplicates_are_not_deduplicated(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(monkeypatch, _failure_payload(project))
    _run_hook(monkeypatch, _failure_payload(project))

    records = _read_log(project)
    assert len(records) == 2
    assert records[0]["eid"] != records[1]["eid"]


# EV-27: 機密情報は伏せてから切り詰めるため、max_excerpt_chars の境界をまたいでも原文が残らない。
def test_secret_straddling_truncation_boundary_is_masked(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path, "max_excerpt_chars: 40\n")
    prefix = "x" * 30 + " "
    _run_hook(
        monkeypatch,
        _failure_payload(
            project,
            tool_input={"command": prefix + API_KEY_SAMPLE},
            error="Exit code 1\n" + prefix + API_KEY_SAMPLE,
        ),
    )

    data = _read_log(project)[0]["data"]
    for value in (data["command"], data["error_excerpt"]):
        assert len(value) <= 40
        assert "abcdefghij" not in value
        assert "sk-abc" not in value


# EV-27: 強化版の範囲（GitHub トークン・PII）も新しい行で伏せられる。
def test_new_event_texts_use_strong_masking_and_pii(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    error = f"Exit code 128\ntoken {GITHUB_TOKEN_SAMPLE} for alice@example.com at /Users/alice/repo"
    _run_hook(monkeypatch, _failure_payload(project, error=error))

    excerpt = _read_log(project)[0]["data"]["error_excerpt"]
    assert GITHUB_TOKEN_SAMPLE not in excerpt
    assert "alice@example.com" not in excerpt
    assert "/Users/alice" not in excerpt


# EV-28: 強化版マスキングが使えないときは command / error_excerpt を空にして記録を続ける。
def test_masking_unavailable_blanks_texts(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    monkeypatch.setattr(capture.masking, "load_masker", lambda *_args, **_kwargs: None)
    _run_hook(monkeypatch, _failure_payload(project, error="Exit code 1\n" + API_KEY_SAMPLE))

    records = _read_log(project)
    assert len(records) == 1
    data = records[0]["data"]
    assert data["command"] == ""
    assert data["error_excerpt"] == ""
    assert data["exit_code"] == 1


# EV-28: 伏せる関数が呼び出しで失敗したときも原文を残さない。
def test_masker_raising_blanks_texts(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)

    def _broken(_text: str) -> str:
        raise RuntimeError("masking failed")

    monkeypatch.setattr(capture.masking, "load_masker", lambda *_args, **_kwargs: _broken)
    _run_hook(monkeypatch, _failure_payload(project, error="Exit code 1\n" + API_KEY_SAMPLE))

    data = _read_log(project)[0]["data"]
    assert data["command"] == ""
    assert data["error_excerpt"] == ""


# EV-29: targets.permission_denied: false で auto mode の拒否を記録しない。
def test_permission_denied_target_toggle(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path, "targets:\n  permission_denied: false\n")
    _run_hook(monkeypatch, _denied_payload(project))

    assert _read_log(project) == []


# EV-08 / EV-29: 既存の種別のトグルは新しいイベントの行にも効く。
def test_existing_target_toggle_applies_to_new_event(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path, "targets:\n  tool_error: false\n")
    _run_hook(monkeypatch, _failure_payload(project))

    assert _read_log(project) == []


# EV-07: enabled: false は新しいイベントの記録も止める。
def test_disabled_stops_new_events(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path, "enabled: false\n")
    _run_hook(monkeypatch, _failure_payload(project))
    _run_hook(monkeypatch, _denied_payload(project))

    assert _read_log(project) == []


# EV-04 / EV-17: 新しい行は従来の項目 + branch + tool_use_id を持つ。
def test_new_event_record_schema(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(monkeypatch, _failure_payload(project))

    record = _read_log(project)[0]
    assert record["v"] == 1
    assert record["type"] == "failure"
    assert record["sid"] == "sess-failure"
    assert set(record["data"]) == LEGACY_DATA_KEYS | {"tool_use_id"}
    assert record["data"]["tool_use_id"] == "toolu_01ABC"


# EV-30: PostToolUse の経路の行は従来の項目だけで、tool_use_id を足さない。
def test_post_tool_use_record_keeps_legacy_schema(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    _run_hook(
        monkeypatch,
        {
            "cwd": str(project),
            "session_id": "sess-legacy",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q"},
            "tool_response": {"stdout": "1 failed", "exit_code": 0},
            "tool_use_id": "toolu_01LEG",
        },
    )

    records = _read_log(project)
    assert len(records) == 1
    assert set(records[0]["data"]) == LEGACY_DATA_KEYS
    assert records[0]["data"]["detected_by"] == "output_pattern"


def _run_subprocess(project: Path, payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CAPTURE_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(project),
        check=False,
        env={"PATH": "/usr/bin:/bin", "AI_ORCHESTRA_DIR": str(REPO_ROOT)},
    )


# EV-11 / EV-17: 新しいイベントでも stdout は無出力で exit 0。
@pytest.mark.parametrize("builder", [_failure_payload, _denied_payload])
def test_new_events_exit_zero_without_stdout(tmp_path, builder) -> None:
    project = _make_project(tmp_path)
    result = _run_subprocess(project, builder(project))

    assert result.returncode == 0
    assert result.stdout == ""
    assert len(_read_log(project)) == 1


# EV-12: 内部例外が起きても exit 0 で終了する。
def test_new_event_internal_exception_exits_zero(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(capture.fd, "classify_command", _raise)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_failure_payload(project))))

    with pytest.raises(SystemExit) as exc_info:
        capture.main()
    assert exc_info.value.code == 0
    assert _read_log(project) == []


# EV-04: tool_use_id は決まった形のときだけ残す（長すぎる・不正な値は空）。
@pytest.mark.parametrize(
    ("value", "expected"),
    [("toolu_01ABC", "toolu_01ABC"), ("x" * 200, ""), ("bad id\n", ""), (None, "")],
)
def test_tool_use_id_is_normalized(monkeypatch, tmp_path, value, expected) -> None:
    project = _make_project(tmp_path)
    _run_hook(monkeypatch, _failure_payload(project, tool_use_id=value))

    assert _read_log(project)[0]["data"]["tool_use_id"] == expected


# EV-12 / NF-06: lib の読み込みに失敗しても、PostToolUse の経路は記録を続け、新しい行は伏せ字なしで空にする。
def test_missing_masking_module_keeps_post_tool_use(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path)
    monkeypatch.setattr(capture, "masking", None)
    _run_hook(
        monkeypatch,
        {
            "cwd": str(project),
            "session_id": "s",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q"},
            "tool_response": {"stdout": "1 failed", "exit_code": 0},
        },
    )
    _run_hook(monkeypatch, _failure_payload(project))

    records = _read_log(project)
    assert len(records) == 2
    assert records[0]["data"]["command"] == "pytest -q"
    assert records[1]["data"]["command"] == ""
    assert records[1]["data"]["error_excerpt"] == ""
