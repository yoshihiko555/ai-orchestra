"""oracle 判定のテスト（Sec1-3 セマンティクス, Sec3-3 pluggable judge backend）。

`command_exit` / `artifact_exists` / `json_schema` は実 shell（`subprocess.run` 既定）を
使うが、`claude`/`codex` は一切呼ばない。`rubric_judge` は runner を完全にフェイクに
差し替える（実 codex/claude プロセスを起動しない）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_oracles",
    "packages/meta-harness/lib/evaluator.py",
)

_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestOracleCommandExit:
    def test_passes_on_exit_zero(self, tmp_path: Path) -> None:
        check = {"id": "c1", "oracle": "command_exit", "command": "true"}
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        assert result == {"id": "c1", "passed": True, "oracle": "command_exit", "detail": "exit=0"}

    def test_fails_on_nonzero_exit(self, tmp_path: Path) -> None:
        check = {"id": "c2", "oracle": "command_exit", "command": "exit 1"}
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        assert result["passed"] is False
        assert "exit=1" in result["detail"]

    def test_runs_in_worktree_cwd(self, tmp_path: Path) -> None:
        (tmp_path / "marker.txt").write_text("hello", encoding="utf-8")
        check = {"id": "c3", "oracle": "command_exit", "command": "test -f marker.txt"}
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        assert result["passed"] is True

    def test_timeout_is_reported_as_failure_not_exception(self, tmp_path: Path) -> None:
        check = {
            "id": "c4",
            "oracle": "command_exit",
            "command": "sleep 5",
            "command_timeout_ms": 50,
        }
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        assert result["passed"] is False
        assert "error" in result["detail"]


class TestOracleArtifactExists:
    def test_passes_when_nonempty_file_matches(self, tmp_path: Path) -> None:
        (tmp_path / "summary.md").write_text("some content", encoding="utf-8")
        check = {"id": "a1", "oracle": "artifact_exists", "path": "summary.md"}
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        assert result["passed"] is True

    def test_fails_when_file_missing(self, tmp_path: Path) -> None:
        check = {"id": "a2", "oracle": "artifact_exists", "path": "does-not-exist.md"}
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        assert result["passed"] is False

    def test_fails_when_file_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "empty.md").write_text("", encoding="utf-8")
        check = {"id": "a3", "oracle": "artifact_exists", "path": "empty.md"}
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        assert result["passed"] is False

    def test_supports_glob_pattern(self, tmp_path: Path) -> None:
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "a.txt").write_text("x", encoding="utf-8")
        check = {"id": "a4", "oracle": "artifact_exists", "path": "out/*.txt"}
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        assert result["passed"] is True


class TestOracleJsonSchema:
    def test_passes_when_valid_against_verdict_schema(self, tmp_path: Path) -> None:
        (tmp_path / "verdict.json").write_text(
            json.dumps({"passed": True, "reason": "ok"}), encoding="utf-8"
        )
        check = {
            "id": "j1",
            "oracle": "json_schema",
            "path": "verdict.json",
            "schema": "verdict.schema.json",
        }
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        assert result["passed"] is True

    def test_fails_when_missing_required_field(self, tmp_path: Path) -> None:
        (tmp_path / "verdict.json").write_text(json.dumps({"passed": True}), encoding="utf-8")
        check = {
            "id": "j2",
            "oracle": "json_schema",
            "path": "verdict.json",
            "schema": "verdict.schema.json",
        }
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        assert result["passed"] is False

    def test_fails_when_file_missing(self, tmp_path: Path) -> None:
        check = {
            "id": "j3",
            "oracle": "json_schema",
            "path": "does-not-exist.json",
            "schema": "verdict.schema.json",
        }
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        assert result["passed"] is False

    def test_fails_when_file_is_not_valid_json(self, tmp_path: Path) -> None:
        (tmp_path / "verdict.json").write_text("not json", encoding="utf-8")
        check = {
            "id": "j4",
            "oracle": "json_schema",
            "path": "verdict.json",
            "schema": "verdict.schema.json",
        }
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        assert result["passed"] is False


class TestOracleUnknown:
    def test_raises_value_error_for_unknown_oracle(self, tmp_path: Path) -> None:
        check = {"id": "u1", "oracle": "not-a-real-oracle"}
        try:
            ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        except ValueError:
            pass
        else:
            raise AssertionError("unknown oracle should raise ValueError")


class TestRubricJudgeCodexBackend:
    _CONFIG = {"judge": {"tool": "codex", "model": None}}

    def test_success_verdict_parsed_from_output_file(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(ev.shutil, "which", lambda name: "/usr/bin/codex")

        def fake_runner(cmd, **kwargs):
            out_index = cmd.index("-o") + 1
            out_path = Path(cmd[out_index])
            out_path.write_text(json.dumps({"passed": True, "reason": "meets rubric"}))
            return _completed(0)

        check = {"id": "r1", "oracle": "rubric_judge", "rubric": "the artifact must exist"}
        result = ev.run_oracle(check, tmp_path, self._CONFIG, _SCHEMA_DIR, runner=fake_runner)
        assert result["passed"] is True
        assert "codex" in result["detail"]

    def test_fail_closed_when_codex_binary_missing(self, tmp_path: Path, monkeypatch) -> None:
        """judge backend が利用不能な場合、check の fail ではなく run 全体の
        verdict=error に伝播させるため、`run_oracle` は EvaluatorStageError を送出する
        （fail-closed, Sec3-3）。"""
        monkeypatch.setattr(ev.shutil, "which", lambda name: None)
        check = {"id": "r2", "oracle": "rubric_judge", "rubric": "..."}
        try:
            ev.run_oracle(check, tmp_path, self._CONFIG, _SCHEMA_DIR, runner=lambda *a, **k: None)
        except ev.EvaluatorStageError as exc:
            assert exc.stage == "oracle"
            assert exc.error_type == "oracle_error"
            assert "judge unavailable" in exc.message
        else:
            raise AssertionError("judge unavailable should raise EvaluatorStageError (fail-closed)")

    def test_fail_closed_on_nonzero_exit(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(ev.shutil, "which", lambda name: "/usr/bin/codex")
        result = ev.run_rubric_judge(
            "rubric",
            tmp_path,
            self._CONFIG,
            _SCHEMA_DIR,
            runner=lambda *a, **k: _completed(1, stderr="boom"),
        )
        assert result.passed is False
        assert result.error is True

    def test_fail_closed_when_output_file_missing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(ev.shutil, "which", lambda name: "/usr/bin/codex")
        result = ev.run_rubric_judge(
            "rubric", tmp_path, self._CONFIG, _SCHEMA_DIR, runner=lambda *a, **k: _completed(0)
        )
        assert result.passed is False
        assert result.error is True

    def test_fail_closed_when_output_file_is_invalid_json(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(ev.shutil, "which", lambda name: "/usr/bin/codex")

        def fake_runner(cmd, **kwargs):
            out_index = cmd.index("-o") + 1
            Path(cmd[out_index]).write_text("not valid json")
            return _completed(0)

        result = ev.run_rubric_judge(
            "rubric", tmp_path, self._CONFIG, _SCHEMA_DIR, runner=fake_runner
        )
        assert result.passed is False
        assert result.error is True

    def test_fail_closed_when_output_does_not_match_verdict_schema(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(ev.shutil, "which", lambda name: "/usr/bin/codex")

        def fake_runner(cmd, **kwargs):
            out_index = cmd.index("-o") + 1
            Path(cmd[out_index]).write_text(json.dumps({"unexpected": "shape"}))
            return _completed(0)

        result = ev.run_rubric_judge(
            "rubric", tmp_path, self._CONFIG, _SCHEMA_DIR, runner=fake_runner
        )
        assert result.passed is False
        assert result.error is True

    def test_prompt_wraps_rubric_with_untrusted_delimiters(self, tmp_path: Path) -> None:
        prompt = ev._build_judge_prompt("do the thing", tmp_path)
        assert ev._JUDGE_DELIMITER_OPEN in prompt
        assert ev._JUDGE_DELIMITER_CLOSE in prompt
        assert "do the thing" in prompt
        assert "not commands" in prompt or "do not follow" in prompt.lower()

    def test_prompt_includes_worktree_absolute_path(self, tmp_path: Path) -> None:
        prompt = ev._build_judge_prompt("check summary.md", tmp_path)
        assert str(tmp_path) in prompt

    def test_prompt_includes_referenced_artifact_excerpt(self, tmp_path: Path) -> None:
        (tmp_path / "summary.md").write_text("candidate wrote this summary", encoding="utf-8")
        prompt = ev._build_judge_prompt("check that summary.md is accurate", tmp_path)
        assert "candidate wrote this summary" in prompt

    def test_prompt_truncates_oversized_artifact_excerpt(self, tmp_path: Path) -> None:
        (tmp_path / "summary.md").write_text("x" * (ev._JUDGE_ARTIFACT_EXCERPT_MAX_CHARS + 500))
        prompt = ev._build_judge_prompt("check summary.md", tmp_path)
        assert "...(truncated)" in prompt


class TestRubricJudgeClaudeBareBackend:
    _CONFIG = {"judge": {"tool": "claude-bare"}}

    def test_fail_closed_when_api_key_missing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(ev, "_api_key_helper_configured", lambda: False)
        result = ev.run_rubric_judge(
            "rubric", tmp_path, self._CONFIG, _SCHEMA_DIR, runner=lambda *a, **k: _completed(0)
        )
        assert result.passed is False
        assert result.error is True
        assert "ANTHROPIC_API_KEY" in result.reason

    def test_succeeds_when_only_api_key_helper_configured(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """ANTHROPIC_API_KEY が無くても `apiKeyHelper` が構成されていれば fail-closed に
        しない（Sec14-1）。"""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(ev, "_api_key_helper_configured", lambda: True)

        def fake_runner(cmd, **kwargs):
            return _completed(0, stdout=json.dumps({"passed": True, "reason": "ok"}))

        result = ev.run_rubric_judge(
            "rubric", tmp_path, self._CONFIG, _SCHEMA_DIR, runner=fake_runner
        )
        assert result.passed is True
        assert result.error is False

    def test_success_when_api_key_present_and_output_parses(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key-for-unit-test-only")

        def fake_runner(cmd, **kwargs):
            assert any("Read(" in arg for arg in cmd)  # path-scoped allowedTools required
            return _completed(0, stdout=json.dumps({"passed": True, "reason": "ok"}))

        result = ev.run_rubric_judge(
            "rubric", tmp_path, self._CONFIG, _SCHEMA_DIR, runner=fake_runner
        )
        assert result.passed is True

    def test_fail_closed_on_nonzero_exit(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key-for-unit-test-only")
        result = ev.run_rubric_judge(
            "rubric",
            tmp_path,
            self._CONFIG,
            _SCHEMA_DIR,
            runner=lambda *a, **k: _completed(1, stderr="boom"),
        )
        assert result.passed is False
        assert result.error is True

    def test_fail_closed_on_unparseable_stdout(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key-for-unit-test-only")
        result = ev.run_rubric_judge(
            "rubric",
            tmp_path,
            self._CONFIG,
            _SCHEMA_DIR,
            runner=lambda *a, **k: _completed(0, stdout="not json"),
        )
        assert result.passed is False
        assert result.error is True


class TestRubricJudgeUnknownBackend:
    def test_unknown_judge_tool_fails_closed(self, tmp_path: Path) -> None:
        config = {"judge": {"tool": "not-a-real-backend"}}
        result = ev.run_rubric_judge(
            "rubric", tmp_path, config, _SCHEMA_DIR, runner=lambda *a, **k: None
        )
        assert result.passed is False
        assert result.error is True
