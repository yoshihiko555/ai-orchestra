"""codex_run.py のテスト。

テスト対象:
- slugify() / build_metadata() の基本動作
- parse_events(): 構造化ペイロード抽出 / agent text フォールバック / 未知イベント無視
- run_validation(): .codex/validation.json のコマンド実行と結果集計
- build_report(): report.md の内容
- main(): preflight/execute_codex をモックした一連の run 生成（events パース→final.json/report.md 保存）
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from tests.module_loader import load_module

codex_run = load_module(
    "codex_run",
    "packages/codex-harness/scripts/codex_run.py",
)


def _trust_validation_json(root: Path, content: str) -> None:
    """Record `content`'s SHA-256 as the trusted ledger hash for validation.json.

    run_validation() now checks .codex/validation.json against the sync
    ledger (R1) before running any commands; tests that exercise the actual
    command-execution path must set up a matching .claude/orchestra.json
    ledger entry, mirroring what sync_codex_files() would record in a real
    install.
    """
    claude_dir = root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    orch = {
        "codex_file_hashes": {
            ".codex/validation.json": hashlib.sha256(content.encode("utf-8")).hexdigest()
        }
    }
    (claude_dir / "orchestra.json").write_text(json.dumps(orch), encoding="utf-8")


class TestSlugify:
    def test_lowercases_and_replaces_non_alnum(self) -> None:
        assert (
            codex_run.slugify("Fix Failing Tests in packages/foo!")
            == "fix-failing-tests-in-packages-foo"
        )

    def test_truncates_to_max_length(self) -> None:
        long_task = "a" * 100
        assert len(codex_run.slugify(long_task)) == codex_run.SLUG_MAX_LENGTH

    def test_falls_back_to_task_when_empty(self) -> None:
        assert codex_run.slugify("!!!") == "task"


class TestParseEvents:
    def test_extracts_schema_shaped_event(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        payload = {
            "status": "success",
            "summary": "did the thing",
            "files_changed": [],
            "validation": [],
            "risks": [],
        }
        events_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        result = codex_run.parse_events(events_path, codex_returncode=0)

        assert result == payload

    def test_falls_back_to_last_agent_text(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        lines = [
            json.dumps({"msg": {"message": "first"}}),
            json.dumps({"msg": {"message": "final message"}}),
        ]
        events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = codex_run.parse_events(events_path, codex_returncode=0)

        assert result["status"] == "success"
        assert result["summary"] == "final message"
        assert result["files_changed"] == []

    def test_marks_failed_status_on_nonzero_exit_without_schema(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        events_path.write_text("", encoding="utf-8")

        result = codex_run.parse_events(events_path, codex_returncode=1)

        assert result["status"] == "failed"

    def test_ignores_unknown_and_malformed_lines(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        lines = [
            "not json at all",
            json.dumps(["array", "not", "object"]),
            json.dumps({"unknown_field": "whatever"}),
            json.dumps({"msg": {"message": "ok"}}),
        ]
        events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = codex_run.parse_events(events_path, codex_returncode=0)

        assert result["summary"] == "ok"

    def test_handles_missing_events_file(self, tmp_path: Path) -> None:
        result = codex_run.parse_events(tmp_path / "missing.jsonl", codex_returncode=0)

        assert result["status"] == "success"
        assert "no structured output" in result["summary"]


class TestExecuteCodex:
    """execute_codex() の subprocess 呼び出し契約（EV-29, EV-30）。"""

    def test_calls_subprocess_with_stdin_devnull_and_sandbox_flag(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr(codex_run.subprocess, "run", fake_run)

        exit_code = codex_run.execute_codex(
            tmp_path, run_dir, "do the thing", "read-only", ["--dangerously-bypass-hook-trust"], 30
        )

        assert exit_code == 0
        assert captured["kwargs"]["stdin"] == codex_run.subprocess.DEVNULL
        assert captured["kwargs"]["timeout"] == 30
        assert "--sandbox" in captured["cmd"]
        assert "read-only" in captured["cmd"]
        assert "--dangerously-bypass-hook-trust" in captured["cmd"]

    def test_returns_124_on_timeout(self, tmp_path: Path, monkeypatch) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

        monkeypatch.setattr(codex_run.subprocess, "run", fake_run)

        exit_code = codex_run.execute_codex(tmp_path, run_dir, "task", "read-only", [], 1)

        assert exit_code == 124
        assert "timed out" in (run_dir / "progress.log").read_text(encoding="utf-8")


class TestRunValidation:
    def test_runs_commands_and_reports_pass_fail(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config = {
            "commands": [
                {"command": "true", "timeout": 5},
                {"command": "false", "timeout": 5},
            ]
        }
        content = json.dumps(config)
        (codex_dir / "validation.json").write_text(content, encoding="utf-8")
        _trust_validation_json(tmp_path, content)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        results = codex_run.run_validation(tmp_path, run_dir)

        assert [r["status"] for r in results] == ["passed", "failed"]
        assert (run_dir / "validation.log").is_file()

    def test_returns_empty_list_when_validation_json_missing(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        results = codex_run.run_validation(tmp_path, run_dir)

        assert results == []
        assert not (run_dir / "validation.log").exists()

    def test_bare_string_entry_does_not_crash(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        content = json.dumps({"commands": ["ruff check ."]})
        (codex_dir / "validation.json").write_text(content, encoding="utf-8")
        _trust_validation_json(tmp_path, content)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        results = codex_run.run_validation(tmp_path, run_dir)

        assert [r["status"] for r in results] == ["failed"]

    def test_list_entry_does_not_crash(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        content = json.dumps({"commands": [["ruff", "check", "."]]})
        (codex_dir / "validation.json").write_text(content, encoding="utf-8")
        _trust_validation_json(tmp_path, content)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        results = codex_run.run_validation(tmp_path, run_dir)

        assert [r["status"] for r in results] == ["failed"]

    def test_string_timeout_is_coerced_and_command_runs(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        content = json.dumps({"commands": [{"command": "true", "timeout": "5"}]})
        (codex_dir / "validation.json").write_text(content, encoding="utf-8")
        _trust_validation_json(tmp_path, content)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        results = codex_run.run_validation(tmp_path, run_dir)

        assert [r["status"] for r in results] == ["passed"]

    def test_empty_command_entry_does_not_crash(self, tmp_path: Path) -> None:
        """R9/R22: an empty (or whitespace-only) `command` must not IndexError
        in `subprocess.run([])` -- it should be reported as a failed entry."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        content = json.dumps({"commands": [{"command": "", "timeout": 5}, {"command": "   "}]})
        (codex_dir / "validation.json").write_text(content, encoding="utf-8")
        _trust_validation_json(tmp_path, content)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        results = codex_run.run_validation(tmp_path, run_dir)

        assert [r["status"] for r in results] == ["failed", "failed"]

    def test_skips_when_validation_json_untrusted(self, tmp_path: Path) -> None:
        """R1: without a matching ledger entry, validation is skipped entirely
        (fail-closed) rather than executing commands from an unverified file."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config = {"commands": [{"command": "true", "timeout": 5}]}
        (codex_dir / "validation.json").write_text(json.dumps(config), encoding="utf-8")
        # No .claude/orchestra.json / ledger entry recorded.
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        results = codex_run.run_validation(tmp_path, run_dir)

        assert len(results) == 1
        assert results[0]["status"] == "skipped"
        assert "untrusted" in results[0]["summary"]
        log = (run_dir / "validation.log").read_text(encoding="utf-8")
        assert "untrusted" in log


class TestBuildReport:
    def test_includes_summary_files_validation_and_risks(self) -> None:
        final_result = {
            "summary": "did the thing",
            "files_changed": [{"path": "a.py", "change_type": "modified", "notes": "fix"}],
            "risks": [{"severity": "low", "description": "minor", "mitigation": "none needed"}],
        }
        validation_results = [{"command": "pytest -q", "status": "passed", "summary": ""}]

        report = codex_run.build_report(
            final_result, validation_results, exit_code=0, duration_seconds=1.2
        )

        assert "did the thing" in report
        assert "a.py" in report
        assert "pytest -q" in report
        assert "minor" in report
        assert "exit code: 0" in report


class TestMainEndToEnd:
    def _init_repo(self, tmp_path: Path) -> Path:
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
        codex_dir = repo_root / ".codex"
        (codex_dir / "schemas").mkdir(parents=True)
        (codex_dir / "hooks.json").write_text("{}", encoding="utf-8")
        (codex_dir / "schemas" / "task_result.schema.json").write_text("{}", encoding="utf-8")
        (codex_dir / "validation.json").write_text(json.dumps({"commands": []}), encoding="utf-8")
        return repo_root

    def test_returns_codex_exit_code_and_writes_artifacts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo_root = self._init_repo(tmp_path)
        monkeypatch.setattr(codex_run, "run_version_gate", lambda label: True)
        monkeypatch.setattr(codex_run, "check_required_codex_files", lambda root, files: [])
        monkeypatch.setattr(
            codex_run,
            "resolve_trust_flags",
            lambda root, allow, label: ["--dangerously-bypass-hook-trust"],
        )

        def fake_execute_codex(repo_root, run_dir, task, sandbox, trust_flags, timeout):
            (run_dir / "events.jsonl").write_text(
                json.dumps({"msg": {"message": "done"}}) + "\n", encoding="utf-8"
            )
            (run_dir / "progress.log").write_text("progress\n", encoding="utf-8")
            return 0

        monkeypatch.setattr(codex_run, "execute_codex", fake_execute_codex)

        exit_code = codex_run.main(["Fix the bug", "--project", str(repo_root)])

        assert exit_code == 0
        run_dirs = list((repo_root / ".codex" / "runs").iterdir())
        assert len(run_dirs) == 1
        run_dir = run_dirs[0]
        assert (run_dir / "prompt.md").read_text(encoding="utf-8") == "Fix the bug\n"
        assert (run_dir / "metadata.json").is_file()
        final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
        assert final["summary"] == "done"
        assert (run_dir / "report.md").is_file()

    def test_returns_one_when_not_in_git_repo(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()

        exit_code = codex_run.main(["task", "--project", str(not_a_repo)])

        assert exit_code == 1

    def test_redacts_secrets_in_diff_validation_and_final_artifacts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """EV-28: redaction must apply on the real write paths: diff.patch,
        validation.log, and final.json (not just in isolated unit calls)."""
        secret = "ghp_" + "a" * 36
        repo_root = self._init_repo(tmp_path)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo_root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo_root, check=True)
        tracked = repo_root / "tracked.txt"
        tracked.write_text("v1\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repo_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_root, check=True)
        tracked.write_text(f"token={secret}\n", encoding="utf-8")

        script_path = repo_root / "print_secret.py"
        script_path.write_text(f"print('token={secret}')\n", encoding="utf-8")
        validation_content = json.dumps(
            {"commands": [{"command": f"python3 {script_path}", "timeout": 10}]}
        )
        (repo_root / ".codex" / "validation.json").write_text(validation_content, encoding="utf-8")
        _trust_validation_json(repo_root, validation_content)

        monkeypatch.setattr(codex_run, "run_version_gate", lambda label: True)
        monkeypatch.setattr(codex_run, "check_required_codex_files", lambda root, files: [])
        monkeypatch.setattr(
            codex_run,
            "resolve_trust_flags",
            lambda root, allow, label: ["--dangerously-bypass-hook-trust"],
        )

        def fake_execute_codex(repo_root, run_dir, task, sandbox, trust_flags, timeout):
            (run_dir / "events.jsonl").write_text(
                json.dumps({"msg": {"message": f"leaked token={secret}"}}) + "\n",
                encoding="utf-8",
            )
            (run_dir / "progress.log").write_text("progress\n", encoding="utf-8")
            return 0

        monkeypatch.setattr(codex_run, "execute_codex", fake_execute_codex)

        exit_code = codex_run.main(["Fix the bug", "--project", str(repo_root)])

        assert exit_code == 0
        run_dir = next((repo_root / ".codex" / "runs").iterdir())

        diff_patch = (run_dir / "diff.patch").read_text(encoding="utf-8")
        assert secret not in diff_patch
        assert "[REDACTED:" in diff_patch

        validation_log = (run_dir / "validation.log").read_text(encoding="utf-8")
        assert secret not in validation_log
        assert "[REDACTED:" in validation_log

        final_json = (run_dir / "final.json").read_text(encoding="utf-8")
        assert secret not in final_json
        assert "[REDACTED:" in final_json

    def test_returns_one_when_trust_verification_fails(self, tmp_path: Path, monkeypatch) -> None:
        repo_root = self._init_repo(tmp_path)
        monkeypatch.setattr(codex_run, "run_version_gate", lambda label: True)
        monkeypatch.setattr(codex_run, "check_required_codex_files", lambda root, files: [])
        monkeypatch.setattr(codex_run, "resolve_trust_flags", lambda root, allow, label: None)

        exit_code = codex_run.main(["task", "--project", str(repo_root)])

        assert exit_code == 1

    def test_returns_one_when_required_codex_files_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """EV-27: missing required .codex files must abort preflight with exit 1."""
        repo_root = self._init_repo(tmp_path)
        monkeypatch.setattr(codex_run, "run_version_gate", lambda label: True)
        monkeypatch.setattr(
            codex_run,
            "check_required_codex_files",
            lambda root, files: [".codex/validation.json"],
        )

        exit_code = codex_run.main(["task", "--project", str(repo_root)])

        assert exit_code == 1
