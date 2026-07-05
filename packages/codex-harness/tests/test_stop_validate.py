"""stop_validate.py のテスト。

テスト対象:
- .codex/validation.json の読み込み（存在しない場合は空リスト）
- .codex/validation.json のハッシュ台帳検証（is_validation_json_trusted）
- コマンド実行結果の集計（passed/failed）、不正エントリの安全な失敗化
- ログファイル生成
- main() が常に exit 0 を返し、失敗時のみ systemMessage を出力すること
- main() が台帳検証を通らない validation.json を実行しないこと
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from tests.module_loader import load_module

stop_validate = load_module(
    "stop_validate",
    "packages/codex-harness/codex/hooks/stop_validate.py",
)


def _write_validation_json(tmp_path: Path, config: dict) -> Path:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(exist_ok=True)
    path = codex_dir / "validation.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _write_trusted_ledger(tmp_path: Path, validation_path: Path) -> None:
    """Write a .claude/orchestra.json ledger whose hash matches validation_path."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(exist_ok=True)
    digest = hashlib.sha256(validation_path.read_bytes()).hexdigest()
    ledger = {"codex_file_hashes": {".codex/validation.json": digest}}
    (claude_dir / "orchestra.json").write_text(json.dumps(ledger), encoding="utf-8")


class TestLoadCommands:
    def test_returns_empty_list_when_file_missing(self, tmp_path: Path) -> None:
        assert stop_validate.load_commands(tmp_path) == []

    def test_loads_commands_from_validation_json(self, tmp_path: Path) -> None:
        _write_validation_json(tmp_path, {"commands": [{"command": "true", "timeout": 5}]})

        commands = stop_validate.load_commands(tmp_path)
        assert commands == [{"command": "true", "timeout": 5}]

    def test_returns_empty_list_for_invalid_json(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "validation.json").write_text("not json", encoding="utf-8")

        assert stop_validate.load_commands(tmp_path) == []


class TestIsValidationJsonTrusted:
    def test_false_when_no_validation_file(self, tmp_path: Path) -> None:
        assert stop_validate.is_validation_json_trusted(tmp_path) is False

    def test_false_when_no_ledger(self, tmp_path: Path) -> None:
        _write_validation_json(tmp_path, {"commands": []})
        assert stop_validate.is_validation_json_trusted(tmp_path) is False

    def test_false_when_hash_mismatch(self, tmp_path: Path) -> None:
        validation_path = _write_validation_json(tmp_path, {"commands": []})
        _write_trusted_ledger(tmp_path, validation_path)
        # Modify the file after recording the ledger hash.
        validation_path.write_text(json.dumps({"commands": [{"command": "rm -rf /"}]}))

        assert stop_validate.is_validation_json_trusted(tmp_path) is False

    def test_true_when_hash_matches(self, tmp_path: Path) -> None:
        validation_path = _write_validation_json(tmp_path, {"commands": []})
        _write_trusted_ledger(tmp_path, validation_path)

        assert stop_validate.is_validation_json_trusted(tmp_path) is True


class TestResolveRepoRoot:
    def test_returns_cwd_when_claude_dir_present(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        assert stop_validate.resolve_repo_root(tmp_path) == tmp_path

    def test_falls_back_to_cwd_when_git_unresolvable(self, tmp_path: Path) -> None:
        # tmp_path has neither .claude nor .git, and (almost certainly) isn't
        # inside a git repo itself, so git rev-parse should fail and we fall
        # back to returning cwd unchanged.
        result = stop_validate.resolve_repo_root(tmp_path)
        assert result == tmp_path


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

    def test_bare_string_entry_is_reported_as_failed(self, tmp_path: Path) -> None:
        result = stop_validate.run_command("true", tmp_path)  # type: ignore[arg-type]
        assert result["passed"] is False

    def test_list_entry_is_reported_as_failed(self, tmp_path: Path) -> None:
        result = stop_validate.run_command(["true"], tmp_path)  # type: ignore[arg-type]
        assert result["passed"] is False

    def test_non_string_command_is_reported_as_failed(self, tmp_path: Path) -> None:
        result = stop_validate.run_command({"command": 123, "timeout": 5}, tmp_path)
        assert result["passed"] is False

    def test_string_timeout_is_coerced_and_command_runs(self, tmp_path: Path) -> None:
        result = stop_validate.run_command({"command": "true", "timeout": "5"}, tmp_path)
        assert result["passed"] is True

    def test_non_numeric_timeout_falls_back_to_default(self, tmp_path: Path) -> None:
        result = stop_validate.run_command({"command": "true", "timeout": "soon"}, tmp_path)
        assert result["passed"] is True

    def test_empty_command_entry_does_not_crash(self, tmp_path: Path) -> None:
        """R9/R22: an empty (or whitespace-only) `command` must not IndexError
        in `subprocess.run([])` -- it should be reported as a failed entry."""
        result = stop_validate.run_command({"command": "", "timeout": 5}, tmp_path)
        assert result["passed"] is False

        result_whitespace = stop_validate.run_command({"command": "   "}, tmp_path)
        assert result_whitespace["passed"] is False


class TestRunCommandsWithBudget:
    """R15: cumulative time budget across all validation commands."""

    def test_runs_all_commands_within_budget(self, tmp_path: Path) -> None:
        commands = [{"command": "true", "timeout": 5}, {"command": "false", "timeout": 5}]
        results = stop_validate.run_commands_with_budget(commands, tmp_path)
        assert [r["passed"] for r in results] == [True, False]

    def test_skips_remaining_commands_once_budget_exhausted(self, tmp_path: Path) -> None:
        commands = [
            {"command": "true", "timeout": 5},
            {"command": "true", "timeout": 5},
            {"command": "false", "timeout": 5},
        ]
        # A budget of 0 is exhausted before the first command even starts.
        results = stop_validate.run_commands_with_budget(commands, tmp_path, total_budget_seconds=0)

        assert len(results) == 3
        assert all(not r["passed"] for r in results)
        assert all(stop_validate.BUDGET_EXCEEDED_MESSAGE in r["output"] for r in results)


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
        (tmp_path / ".claude").mkdir()
        payload = json.dumps({"cwd": str(tmp_path)})
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        assert stop_validate.main() == 0
        assert not (tmp_path / ".codex" / "reports").exists()

    def test_reports_failure_but_still_exits_zero(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        validation_path = _write_validation_json(
            tmp_path, {"commands": [{"command": "false", "timeout": 5}]}
        )
        _write_trusted_ledger(tmp_path, validation_path)

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
        validation_path = _write_validation_json(
            tmp_path, {"commands": [{"command": "true", "timeout": 5}]}
        )
        _write_trusted_ledger(tmp_path, validation_path)

        payload = json.dumps({"cwd": str(tmp_path)})
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))

        assert stop_validate.main() == 0
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_skips_validation_when_ledger_missing(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        (tmp_path / ".claude").mkdir()
        _write_validation_json(tmp_path, {"commands": [{"command": "false", "timeout": 5}]})
        # No .claude/orchestra.json ledger written.

        payload = json.dumps({"cwd": str(tmp_path)})
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))

        assert stop_validate.main() == 0
        assert not (tmp_path / ".codex" / "reports").exists()

        captured = capsys.readouterr()
        emitted = json.loads(captured.out)
        assert "not trusted" in emitted["systemMessage"]

    def test_skips_validation_when_hash_mismatch(self, tmp_path: Path, monkeypatch, capsys) -> None:
        validation_path = _write_validation_json(
            tmp_path, {"commands": [{"command": "false", "timeout": 5}]}
        )
        _write_trusted_ledger(tmp_path, validation_path)
        # Modify validation.json after the ledger hash was recorded.
        validation_path.write_text(
            json.dumps({"commands": [{"command": "rm -rf /", "timeout": 5}]})
        )

        payload = json.dumps({"cwd": str(tmp_path)})
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))

        assert stop_validate.main() == 0
        assert not (tmp_path / ".codex" / "reports").exists()

        captured = capsys.readouterr()
        emitted = json.loads(captured.out)
        assert "not trusted" in emitted["systemMessage"]
