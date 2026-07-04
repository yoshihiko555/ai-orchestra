"""stop_validate.py のテスト。

テスト対象:
- .codex/validation.json の読み込み（存在しない場合は空リスト）
- コマンド実行結果の集計（passed/failed）
- ログファイル生成
- main() が常に exit 0 を返し、失敗時のみ systemMessage を出力すること
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from tests.module_loader import load_module

stop_validate = load_module(
    "stop_validate",
    "packages/codex-harness/codex/hooks/stop_validate.py",
)


class TestLoadCommands:
    def test_returns_empty_list_when_file_missing(self, tmp_path: Path) -> None:
        assert stop_validate.load_commands(tmp_path) == []

    def test_loads_commands_from_validation_json(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config = {"commands": [{"command": "true", "timeout": 5}]}
        (codex_dir / "validation.json").write_text(json.dumps(config), encoding="utf-8")

        commands = stop_validate.load_commands(tmp_path)
        assert commands == [{"command": "true", "timeout": 5}]

    def test_returns_empty_list_for_invalid_json(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "validation.json").write_text("not json", encoding="utf-8")

        assert stop_validate.load_commands(tmp_path) == []


class TestRunCommand:
    def test_passed_command(self, tmp_path: Path) -> None:
        result = stop_validate.run_command({"command": "true", "timeout": 5}, tmp_path)
        assert result["passed"] is True

    def test_failed_command(self, tmp_path: Path) -> None:
        result = stop_validate.run_command({"command": "false", "timeout": 5}, tmp_path)
        assert result["passed"] is False

    def test_missing_binary_is_reported_as_failed(self, tmp_path: Path) -> None:
        result = stop_validate.run_command(
            {"command": "definitely-not-a-real-binary", "timeout": 5}, tmp_path
        )
        assert result["passed"] is False


class TestBuildSummary:
    def test_none_when_all_passed(self) -> None:
        results = [{"command": "true", "passed": True, "output": ""}]
        assert stop_validate.build_summary(results) is None

    def test_lists_failed_commands(self) -> None:
        results = [
            {"command": "ruff check .", "passed": False, "output": "err"},
            {"command": "pytest -q", "passed": True, "output": ""},
        ]
        summary = stop_validate.build_summary(results)
        assert summary is not None
        assert "ruff check ." in summary
        assert "pytest -q" not in summary


class TestWriteLog:
    def test_creates_log_file(self, tmp_path: Path) -> None:
        results = [{"command": "true", "passed": True, "output": "ok"}]
        log_path = stop_validate.write_log(tmp_path, results)
        assert log_path.exists()
        assert "true" in log_path.read_text(encoding="utf-8")


class TestMain:
    def test_exits_zero_when_no_validation_file(self, tmp_path: Path, monkeypatch) -> None:
        payload = json.dumps({"cwd": str(tmp_path)})
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        assert stop_validate.main() == 0
        assert not (tmp_path / ".codex" / "reports").exists()

    def test_reports_failure_but_still_exits_zero(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config = {"commands": [{"command": "false", "timeout": 5}]}
        (codex_dir / "validation.json").write_text(json.dumps(config), encoding="utf-8")

        payload = json.dumps({"cwd": str(tmp_path)})
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))

        assert stop_validate.main() == 0

        reports_dir = tmp_path / ".codex" / "reports"
        assert reports_dir.exists()
        assert list(reports_dir.glob("validation-*.log"))

        captured = capsys.readouterr()
        emitted = json.loads(captured.out)
        assert "false" in emitted["systemMessage"]

    def test_silent_when_all_pass(self, tmp_path: Path, monkeypatch, capsys) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config = {"commands": [{"command": "true", "timeout": 5}]}
        (codex_dir / "validation.json").write_text(json.dumps(config), encoding="utf-8")

        payload = json.dumps({"cwd": str(tmp_path)})
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))

        assert stop_validate.main() == 0
        captured = capsys.readouterr()
        assert captured.out == ""
