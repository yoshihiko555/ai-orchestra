"""codex_review.py のテスト。

テスト対象:
- diff が空の場合は codex を呼ばず中断する
- parse_events(): 構造化ペイロード抽出 / フォールバック
- build_report(): severity 順の findings 一覧
- main(): execute_codex_review をモックした一連の review 生成
- read-only 実行中に git status が変化した場合の警告
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.module_loader import load_module

codex_review = load_module(
    "codex_review",
    "packages/codex-harness/scripts/codex_review.py",
)


class TestParseEvents:
    def test_extracts_schema_shaped_event(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        payload = {"status": "success", "summary": "looks good", "findings": []}
        events_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        result = codex_review.parse_events(events_path, codex_returncode=0)

        assert result == payload

    def test_falls_back_to_agent_text(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        events_path.write_text(
            json.dumps({"msg": {"message": "no issues"}}) + "\n", encoding="utf-8"
        )

        result = codex_review.parse_events(events_path, codex_returncode=0)

        assert result["summary"] == "no issues"
        assert result["findings"] == []

    def test_missing_events_file_yields_synthetic_result(self, tmp_path: Path) -> None:
        result = codex_review.parse_events(tmp_path / "missing.jsonl", codex_returncode=1)

        assert result["status"] == "failed"


class TestExecuteCodexReview:
    """execute_codex_review() の subprocess 呼び出し契約（EV-31）。"""

    def test_calls_subprocess_with_read_only_sandbox_and_diff_stdin(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        input_diff_path = run_dir / "input.diff"
        input_diff_path.write_text("diff --git a/x b/x\n", encoding="utf-8")
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr(codex_review.subprocess, "run", fake_run)

        exit_code = codex_review.execute_codex_review(
            tmp_path, run_dir, input_diff_path, ["--dangerously-bypass-hook-trust"], 30
        )

        assert exit_code == 0
        assert "--sandbox" in captured["cmd"]
        assert "read-only" in captured["cmd"]
        assert "--output-schema" in captured["cmd"]
        assert codex_review.SCHEMA_REL_PATH in captured["cmd"]
        # stdin must be the opened diff file (not DEVNULL, not the sandbox arg)
        assert captured["kwargs"]["stdin"].name == str(input_diff_path)


class TestBuildReport:
    def test_sorts_findings_by_severity(self) -> None:
        review_result = {
            "status": "success",
            "summary": "overview",
            "findings": [
                {
                    "severity": "low",
                    "file": "a.py",
                    "line": 1,
                    "rationale": "r1",
                    "suggested_fix": "f1",
                },
                {
                    "severity": "critical",
                    "file": "b.py",
                    "line": 2,
                    "rationale": "r2",
                    "suggested_fix": "f2",
                },
            ],
        }

        report = codex_review.build_report(review_result, write_warning=None)

        critical_index = report.index("b.py")
        low_index = report.index("a.py")
        assert critical_index < low_index

    def test_reports_none_when_no_findings(self) -> None:
        review_result = {"status": "success", "summary": "clean", "findings": []}
        report = codex_review.build_report(review_result, write_warning=None)
        assert "(none reported)" in report

    def test_includes_write_warning(self) -> None:
        review_result = {"status": "success", "summary": "clean", "findings": []}
        report = codex_review.build_report(review_result, write_warning="unexpected write detected")
        assert "unexpected write detected" in report


class TestMainEmptyDiff:
    def _init_repo_with_no_diff(self, tmp_path: Path) -> Path:
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
        codex_dir = repo_root / ".codex"
        (codex_dir / "schemas").mkdir(parents=True)
        (codex_dir / "hooks.json").write_text("{}", encoding="utf-8")
        (codex_dir / "schemas" / "review_result.schema.json").write_text("{}", encoding="utf-8")
        return repo_root

    def test_returns_zero_and_skips_codex_when_diff_empty(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo_root = self._init_repo_with_no_diff(tmp_path)
        monkeypatch.setattr(codex_review, "run_version_gate", lambda label: True)
        monkeypatch.setattr(codex_review, "check_required_codex_files", lambda root, files: [])
        monkeypatch.setattr(
            codex_review,
            "resolve_trust_flags",
            lambda root, allow, label: ["--dangerously-bypass-hook-trust"],
        )

        def fail_if_called(*args, **kwargs):
            raise AssertionError("execute_codex_review must not be called for an empty diff")

        monkeypatch.setattr(codex_review, "execute_codex_review", fail_if_called)

        exit_code = codex_review.main(["--base", "main", "--project", str(repo_root)])

        assert exit_code == 0
        run_dirs = list((repo_root / ".codex" / "runs").iterdir())
        assert len(run_dirs) == 1
        assert (run_dirs[0] / "input.diff").read_text(encoding="utf-8") == ""
        assert not (run_dirs[0] / "review.json").exists()

    def test_returns_one_when_not_in_git_repo(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()

        exit_code = codex_review.main(["--project", str(not_a_repo)])

        assert exit_code == 1

    def test_returns_one_when_trust_verification_fails(self, tmp_path: Path, monkeypatch) -> None:
        """EV-24: untrusted hooks + allow_untrusted=False must abort with exit 1."""
        repo_root = self._init_repo_with_no_diff(tmp_path)
        monkeypatch.setattr(codex_review, "run_version_gate", lambda label: True)
        monkeypatch.setattr(codex_review, "check_required_codex_files", lambda root, files: [])
        monkeypatch.setattr(codex_review, "resolve_trust_flags", lambda root, allow, label: None)

        exit_code = codex_review.main(["--project", str(repo_root)])

        assert exit_code == 1

    def test_returns_one_when_required_codex_files_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """EV-27: missing required .codex files must abort preflight with exit 1."""
        repo_root = self._init_repo_with_no_diff(tmp_path)
        monkeypatch.setattr(codex_review, "run_version_gate", lambda label: True)
        monkeypatch.setattr(
            codex_review,
            "check_required_codex_files",
            lambda root, files: [".codex/schemas/review_result.schema.json"],
        )

        exit_code = codex_review.main(["--project", str(repo_root)])

        assert exit_code == 1


class TestMainWithDiff:
    def _init_repo_with_diff(self, tmp_path: Path) -> Path:
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo_root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo_root, check=True)
        (repo_root / "a.txt").write_text("v1\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=repo_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_root, check=True)
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=True)
        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo_root, check=True)
        (repo_root / "a.txt").write_text("v2\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=repo_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "change"], cwd=repo_root, check=True)

        codex_dir = repo_root / ".codex"
        (codex_dir / "schemas").mkdir(parents=True)
        (codex_dir / "hooks.json").write_text("{}", encoding="utf-8")
        (codex_dir / "schemas" / "review_result.schema.json").write_text("{}", encoding="utf-8")
        return repo_root

    def test_generates_review_artifacts_for_nonempty_diff(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo_root = self._init_repo_with_diff(tmp_path)
        monkeypatch.setattr(codex_review, "run_version_gate", lambda label: True)
        monkeypatch.setattr(codex_review, "check_required_codex_files", lambda root, files: [])
        monkeypatch.setattr(
            codex_review,
            "resolve_trust_flags",
            lambda root, allow, label: ["--dangerously-bypass-hook-trust"],
        )

        def fake_execute_codex_review(repo_root, run_dir, input_diff_path, trust_flags, timeout):
            payload = {"status": "success", "summary": "reviewed", "findings": []}
            (run_dir / "events.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")
            (run_dir / "progress.log").write_text("progress\n", encoding="utf-8")
            return 0

        monkeypatch.setattr(codex_review, "execute_codex_review", fake_execute_codex_review)

        exit_code = codex_review.main(["--base", "main", "--project", str(repo_root)])

        assert exit_code == 0
        run_dirs = [
            d for d in (repo_root / ".codex" / "runs").iterdir() if d.name.endswith("-review")
        ]
        assert len(run_dirs) == 1
        review = json.loads((run_dirs[0] / "review.json").read_text(encoding="utf-8"))
        assert review["summary"] == "reviewed"
        assert (run_dirs[0] / "report.md").is_file()
