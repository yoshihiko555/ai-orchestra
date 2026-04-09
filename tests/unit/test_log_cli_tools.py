"""log-cli-tools.py のユニットテスト。"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

core_hooks_dir = str(REPO_ROOT / "packages" / "core" / "hooks")
if core_hooks_dir not in sys.path:
    sys.path.insert(0, core_hooks_dir)

log_cli_tools = load_module(
    "log_cli_tools_test", "packages/cli-logging/hooks/log-cli-tools.py"
)


def _make_stdin(data: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """stdin を JSON 入力で置き換える。"""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))


class TestPromptExtraction:
    """プロンプト抽出ヘルパーのテスト。"""

    def test_extract_codex_prompt(self) -> None:
        """`codex exec` から prompt を抽出する。"""
        command = 'codex exec --model gpt-5.3-codex --full-auto "diagnose failing tests"'
        assert log_cli_tools.extract_codex_prompt(command) == "diagnose failing tests"

    def test_extract_gemini_prompt(self) -> None:
        """`gemini -p` から prompt を抽出する。"""
        command = 'gemini -m gemini-pro -p "research this bug"'
        assert log_cli_tools.extract_gemini_prompt(command) == "research this bug"

    def test_extract_model_for_both_tools(self) -> None:
        """tool ごとの model フラグを抽出する。"""
        codex_command = 'codex exec --model gpt-5.4 "hello"'
        gemini_command = 'gemini -m gemini-3.1-pro -p "hello"'

        assert log_cli_tools.extract_model(codex_command) == "gpt-5.4"
        assert log_cli_tools.extract_model(gemini_command, tool="gemini") == "gemini-3.1-pro"


class TestMain:
    """main のテスト。"""

    def test_logs_codex_call_with_default_model_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--model` 省略時は設定のデフォルトモデルで記録する。"""
        project_dir = tmp_path / "project"
        work_dir = project_dir / "app"
        (project_dir / ".claude").mkdir(parents=True)
        work_dir.mkdir(parents=True)
        monkeypatch.chdir(work_dir)
        monkeypatch.setattr(
            log_cli_tools,
            "load_package_config",
            lambda *args: {"codex": {"model": "gpt-default"}},
        )
        _make_stdin(
            {
                "tool_name": "Bash",
                "cwd": str(project_dir),
                "tool_input": {"command": 'codex exec --full-auto "diagnose issue"'},
                "tool_response": {"exit_code": 0, "stdout": "Root cause found"},
            },
            monkeypatch,
        )

        log_cli_tools.main()

        captured = capsys.readouterr()
        assert "[LOG] Codex call logged" in captured.out
        log_file = project_dir / ".claude" / "logs" / "cli-tools.jsonl"
        entry = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert entry["tool"] == "codex"
        assert entry["model"] == "gpt-default"
        assert entry["prompt"] == "diagnose issue"
        assert entry["response"] == "Root cause found"
        assert entry["success"] is True

    def test_logs_gemini_call_with_explicit_model(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Gemini は `-m` を優先して記録する。"""
        monkeypatch.chdir(tmp_path)
        _make_stdin(
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": 'gemini -m gemini-3.1-pro -p "research session cleanup"'
                },
                "tool_response": {"exit_code": 0, "stdout": "analysis"},
            },
            monkeypatch,
        )

        log_cli_tools.main()

        captured = capsys.readouterr()
        assert "[LOG] Gemini call logged" in captured.out
        log_file = tmp_path / ".claude" / "logs" / "cli-tools.jsonl"
        entry = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert entry["tool"] == "gemini"
        assert entry["model"] == "gemini-3.1-pro"
        assert entry["prompt"] == "research session cleanup"

    def test_ignores_non_cli_bash_command(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Codex/Gemini 以外の Bash は記録しない。"""
        monkeypatch.chdir(tmp_path)
        _make_stdin(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "pytest -q"},
                "tool_response": {"exit_code": 0, "stdout": "passed"},
            },
            monkeypatch,
        )

        log_cli_tools.main()

        captured = capsys.readouterr()
        assert captured.out == ""
        assert not (tmp_path / ".claude" / "logs" / "cli-tools.jsonl").exists()
