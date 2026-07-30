from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from tests.module_loader import load_module

lint_on_save = load_module("lint_on_save", "packages/quality-gates/hooks/lint-on-save.py")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app/main.py", "python"),
        ("frontend/app.ts", "javascript"),
        ("docs/guide.md", "prettier"),
        ("config/settings.yaml", "prettier"),
        ("cmd/main.go", "go"),
        ("src/lib.rs", "rust"),
    ],
)
def test_get_file_kind_detects_supported_extensions(path: str, expected: str) -> None:
    assert lint_on_save.get_file_kind(path) == expected


def test_get_file_kind_detects_shell_script_by_shebang(tmp_path: Path) -> None:
    script = tmp_path / "deploy"
    script.write_text("#!/bin/bash\necho hello\n", encoding="utf-8")

    assert lint_on_save.get_file_kind(str(script)) == "shell"


def test_get_file_kind_returns_none_for_unsupported_file() -> None:
    assert lint_on_save.get_file_kind("notes.txt") is None


def test_build_lint_steps_for_python() -> None:
    steps = lint_on_save.build_lint_steps("app/main.py")
    assert [step["name"] for step in steps] == ["ruff format", "ruff check"]


def test_build_lint_steps_for_typescript() -> None:
    steps = lint_on_save.build_lint_steps("frontend/app.ts")
    assert [step["name"] for step in steps] == ["biome check", "prettier", "eslint"]


def test_build_lint_steps_for_shell() -> None:
    steps = lint_on_save.build_lint_steps("scripts/deploy.sh")
    assert [step["name"] for step in steps] == ["shfmt", "shellcheck"]


def test_run_step_skips_missing_tool_errors(monkeypatch) -> None:
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    responses = iter(
        [
            Result(1, stderr="npm ERR! could not determine executable to run"),
            Result(0, stdout="formatted"),
        ]
    )

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return next(responses)

    monkeypatch.setattr(lint_on_save.subprocess, "run", fake_run)

    result = lint_on_save.run_step(
        {
            "name": "prettier",
            "commands": [["npm", "exec", "--", "prettier", "--write", "file.ts"], ["prettier"]],
        },
        ".",
    )

    assert result == {"name": "prettier", "success": True, "output": "formatted"}
    assert len(calls) == 2


def test_run_step_falls_back_on_timeout(monkeypatch) -> None:
    """EV-14: 15秒タイムアウト（subprocess.TimeoutExpired）でも次候補にフォールバックする。"""
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        if cmd[0] == "pnpm":
            raise lint_on_save.subprocess.TimeoutExpired(cmd=cmd, timeout=15)
        return Result(0, stdout="formatted")

    monkeypatch.setattr(lint_on_save.subprocess, "run", fake_run)

    result = lint_on_save.run_step(
        {
            "name": "prettier",
            "commands": [
                ["pnpm", "exec", "prettier", "--write", "file.ts"],
                ["npm", "exec", "--", "prettier", "--write", "file.ts"],
            ],
        },
        ".",
    )

    assert result == {"name": "prettier", "success": True, "output": "formatted"}
    assert len(calls) == 2


def test_main_skips_unsupported_files(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "packages/quality-gates/manifest.txt"},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))

    with pytest.raises(SystemExit) as exc_info:
        lint_on_save.main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == ""


def test_main_reports_lint_result(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "packages/quality-gates/hooks/lint-on-save.py"},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    monkeypatch.setattr(
        lint_on_save,
        "run_lint_commands",
        lambda _: [{"name": "ruff format", "success": True, "output": "1 file reformatted"}],
    )

    with pytest.raises(SystemExit) as exc_info:
        lint_on_save.main()

    assert exc_info.value.code == 0
    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[Lint OK]" in context
    assert "ruff format: 1 file reformatted" in context


# ---------------------------------------------------------------------------
# EV-21: quality_gate.enabled 遵守
# ---------------------------------------------------------------------------


def test_main_no_op_when_quality_gate_disabled(
    monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """quality_gate.enabled=false のときは formatter/linter 実行を含む全動作を行わない。"""
    payload = {
        "tool_name": "Write",
        "cwd": "/project",
        "tool_input": {"file_path": "packages/quality-gates/hooks/lint-on-save.py"},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    monkeypatch.setattr(
        lint_on_save,
        "load_package_config",
        lambda *_args: {"features": {"quality_gate": {"enabled": False}}},
    )
    called = {"ran": False}

    def _fail_if_called(_file_path):  # type: ignore[no-untyped-def]
        called["ran"] = True
        return []

    monkeypatch.setattr(lint_on_save, "run_lint_commands", _fail_if_called)

    with pytest.raises(SystemExit) as exc_info:
        lint_on_save.main()

    assert exc_info.value.code == 0
    assert called["ran"] is False
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# EV-22: additionalContext の秘匿情報マスキング
# ---------------------------------------------------------------------------


def test_main_masks_secrets_in_lint_output(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "tool_name": "Write",
        "cwd": "/project",
        "tool_input": {"file_path": "packages/quality-gates/hooks/lint-on-save.py"},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    monkeypatch.setattr(
        lint_on_save,
        "load_package_config",
        lambda *_args: {"features": {"quality_gate": {"enabled": True}}},
    )
    monkeypatch.setattr(
        lint_on_save,
        "run_lint_commands",
        lambda _: [
            {
                "name": "ruff check",
                "success": False,
                "output": "config error: api_key=sk-1234567890abcdefghijklmno",
            }
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        lint_on_save.main()

    assert exc_info.value.code == 0
    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "sk-1234567890abcdefghijklmno" not in context
    assert "[REDACTED]" in context


# ---------------------------------------------------------------------------
# EV-10: main() の fail-open（例外捕捉 → stderr ログ + exit 0）
# ---------------------------------------------------------------------------


def test_main_fails_open_on_unexpected_exception(
    monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {
        "tool_name": "Write",
        "cwd": "/project",
        "tool_input": {"file_path": "packages/quality-gates/hooks/lint-on-save.py"},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))

    def _raise(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(lint_on_save, "load_package_config", _raise)

    with pytest.raises(SystemExit) as exc_info:
        lint_on_save.main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Hook error" in captured.err
    assert "boom" in captured.err
