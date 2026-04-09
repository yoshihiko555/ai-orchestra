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
