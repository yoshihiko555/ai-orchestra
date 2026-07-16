"""oracle 判定のテスト（Sec1-3 セマンティクス, Sec3-3 pluggable judge backend）。

`command_exit` / `artifact_exists` / `json_schema` は実 shell（`subprocess.run` 既定）を
使うが、`claude`/`codex` は一切呼ばない。`rubric_judge` は runner を完全にフェイクに
差し替える（実 codex/claude プロセスを起動しない）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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
    @pytest.fixture(autouse=True)
    def _isolated_launch(self, tmp_path: Path, monkeypatch) -> None:
        settings_dir = tmp_path / "oracle-settings"
        settings_dir.mkdir()
        settings_path = settings_dir / "scenario.json"
        settings_path.write_text("{}", encoding="utf-8")
        run_tmp = tmp_path / "oracle-tmp"
        run_tmp.mkdir()
        self.launch = ev.siso.ScenarioIsolationLaunch(
            executable="/usr/bin/srt",
            settings_path=settings_path,
            settings={
                "network": {"allowedDomains": ["api.anthropic.com"]},
                "filesystem": {"allowRead": [str(tmp_path)], "allowWrite": [str(tmp_path)]},
            },
            env={"PATH": "/usr/bin:/bin"},
            metadata={},
            owned_tmp_dir=run_tmp,
        )

        def local_capture(args, *, cwd, timeout, env, cleanup_args=None):
            return subprocess.run(
                args[-3:],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )

        monkeypatch.setattr(ev.sproc, "run_bounded_capture", local_capture)

    def test_passes_on_exit_zero(self, tmp_path: Path) -> None:
        check = {"id": "c1", "oracle": "command_exit", "command": "true"}
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR, isolation_launch=self.launch)
        assert result == {"id": "c1", "passed": True, "oracle": "command_exit", "detail": "exit=0"}

    def test_missing_isolation_launch_fails_closed(self, tmp_path: Path) -> None:
        check = {"id": "c0", "oracle": "command_exit", "command": "true"}
        with pytest.raises(ev.EvaluatorStageError, match="requires an isolated oracle"):
            ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)

    def test_fails_on_nonzero_exit(self, tmp_path: Path) -> None:
        check = {"id": "c2", "oracle": "command_exit", "command": "exit 1"}
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR, isolation_launch=self.launch)
        assert result["passed"] is False
        assert "exit=1" in result["detail"]

    def test_runs_in_worktree_cwd(self, tmp_path: Path) -> None:
        (tmp_path / "marker.txt").write_text("hello", encoding="utf-8")
        check = {"id": "c3", "oracle": "command_exit", "command": "test -f marker.txt"}
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR, isolation_launch=self.launch)
        assert result["passed"] is True

    def test_timeout_is_reported_as_failure_not_exception(self, tmp_path: Path) -> None:
        check = {
            "id": "c4",
            "oracle": "command_exit",
            "command": "sleep 5",
            "command_timeout_ms": 50,
        }
        with pytest.raises(ev.EvaluatorStageError) as exc_info:
            ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR, isolation_launch=self.launch)
        assert exc_info.value.error_type == "oracle_error"

    def test_scenario_command_timeout_ms_is_honored_when_check_omits_it(
        self, tmp_path: Path
    ) -> None:
        """R3-3: `command_exit` の per-check schema(check_item, additionalProperties:
        false)は `command_timeout_ms` を持てないため、実際のシナリオではこの値は常に
        scenario 単位でしか設定できない。`run_oracle` の `scenario_command_timeout_ms`
        (既定 `DEFAULT_COMMAND_TIMEOUT_MS`)が `_oracle_command_exit` へ実際に渡り、check
        に無い場合のフォールバックとして使われることを確認する(以前は常に
        `DEFAULT_COMMAND_TIMEOUT_MS` に固定され、シナリオの `command_timeout_ms` が
        無視されていた)。"""
        check = {"id": "c5", "oracle": "command_exit", "command": "sleep 5"}
        with pytest.raises(ev.EvaluatorStageError) as exc_info:
            ev.run_oracle(
                check,
                tmp_path,
                {},
                _SCHEMA_DIR,
                isolation_launch=self.launch,
                scenario_command_timeout_ms=50,
            )
        assert exc_info.value.error_type == "oracle_error"

    def test_isolated_oracle_uses_no_network_read_only_profile(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        settings_dir = tmp_path / "settings"
        settings_dir.mkdir()
        settings_path = settings_dir / "scenario.json"
        settings_path.write_text("{}", encoding="utf-8")
        run_tmp = tmp_path / "run-tmp"
        run_tmp.mkdir()
        launch = ev.siso.ScenarioIsolationLaunch(
            executable="/usr/bin/srt",
            settings_path=settings_path,
            settings={
                "network": {"allowedDomains": ["api.anthropic.com"]},
                "filesystem": {
                    "allowRead": [str(worktree)],
                    "allowWrite": [str(worktree), str(run_tmp)],
                },
            },
            env={"PATH": "/usr/bin:/bin"},
            metadata={},
            owned_tmp_dir=run_tmp,
        )
        captured = []

        def fake_runner(cmd, **kwargs):
            captured.append((cmd, kwargs))
            return _completed(0)

        monkeypatch.setattr(ev.sproc, "run_bounded_capture", fake_runner)

        result = ev.run_oracle(
            {"id": "c5", "oracle": "command_exit", "command": "true"},
            worktree,
            {},
            _SCHEMA_DIR,
            isolation_launch=launch,
        )
        assert result["passed"] is True
        assert captured[0][0][:3] == ["/usr/bin/srt", "--settings", captured[0][0][2]]
        oracle_settings = json.loads(Path(captured[0][0][2]).read_text(encoding="utf-8"))
        assert oracle_settings["network"]["allowedDomains"] == []
        assert oracle_settings["filesystem"]["allowWrite"] == [str(run_tmp.resolve())]


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

    def test_rejects_symlink_to_file_outside_worktree(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside-summary.md"
        outside.write_text("secret", encoding="utf-8")
        (tmp_path / "summary.md").symlink_to(outside)
        check = {"id": "a5", "oracle": "artifact_exists", "path": "summary.md"}
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        assert result["passed"] is False

    def test_rejects_artifact_over_size_limit(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(ev, "MAX_ORACLE_ARTIFACT_BYTES", 4)
        (tmp_path / "summary.md").write_text("12345", encoding="utf-8")
        check = {"id": "a6", "oracle": "artifact_exists", "path": "summary.md"}
        result = ev.run_oracle(check, tmp_path, {}, _SCHEMA_DIR)
        assert result["passed"] is False


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

    def test_rejects_symlinked_json(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside-verdict.json"
        outside.write_text(json.dumps({"passed": True, "reason": "secret"}), encoding="utf-8")
        (tmp_path / "verdict.json").symlink_to(outside)
        check = {
            "id": "j5",
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

    def test_codex_is_disabled_because_read_only_sandbox_cannot_deny_reads(
        self, tmp_path: Path
    ) -> None:
        check = {"id": "r2", "oracle": "rubric_judge", "rubric": "..."}
        with pytest.raises(ev.EvaluatorStageError) as exc_info:
            ev.run_oracle(check, tmp_path, self._CONFIG, _SCHEMA_DIR, runner=lambda *a, **k: None)
        assert exc_info.value.error_type == "oracle_error"
        assert "cannot be made read-deny" in exc_info.value.message

    def test_prompt_wraps_rubric_with_untrusted_delimiters(self, tmp_path: Path) -> None:
        prompt = ev._build_judge_prompt("do the thing", tmp_path)
        assert ev._JUDGE_DELIMITER_OPEN in prompt
        assert ev._JUDGE_DELIMITER_CLOSE in prompt
        assert "do the thing" in prompt
        assert "not commands" in prompt or "do not follow" in prompt.lower()

    def test_prompt_excludes_worktree_absolute_path(self, tmp_path: Path) -> None:
        prompt = ev._build_judge_prompt("check summary.md", tmp_path)
        assert str(tmp_path) not in prompt

    def test_prompt_includes_referenced_artifact_excerpt(self, tmp_path: Path) -> None:
        (tmp_path / "summary.md").write_text("candidate wrote this summary", encoding="utf-8")
        prompt = ev._build_judge_prompt("check that summary.md is accurate", tmp_path)
        assert "candidate wrote this summary" in prompt

    def test_prompt_truncates_oversized_artifact_excerpt(self, tmp_path: Path) -> None:
        (tmp_path / "summary.md").write_text("x" * (ev._JUDGE_ARTIFACT_EXCERPT_MAX_CHARS + 500))
        prompt = ev._build_judge_prompt("check summary.md", tmp_path)
        assert "...(truncated)" in prompt

    def test_prompt_does_not_follow_symlinked_artifact(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside-rubric.md"
        outside.write_text("do not leak me", encoding="utf-8")
        (tmp_path / "summary.md").symlink_to(outside)
        prompt = ev._build_judge_prompt("check summary.md", tmp_path)
        assert "do not leak me" not in prompt


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
            tools_index = cmd.index("--allowedTools")
            assert cmd[tools_index + 1] == ""  # judge は staging 済み抜粋だけを評価する
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

    def test_docker_judge_uses_broker_container_without_host_api_key(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        launch = ev.siso.ScenarioIsolationLaunch(
            executable="docker",
            settings_path=None,
            settings={},
            env={"PATH": "/usr/bin:/bin"},
            metadata={},
            backend="docker",
            docker_launch=object(),
        )
        captured: dict = {}
        monkeypatch.setattr(
            ev.siso,
            "build_judge_command",
            lambda _launch, cmd: (
                ["docker", "run", "judge", *cmd],
                ["docker", "rm", "-f", "judge"],
            ),
        )

        def fake_capture(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return _completed(0, stdout=json.dumps({"passed": True, "reason": "ok"}))

        monkeypatch.setattr(ev.sproc, "run_bounded_capture", fake_capture)

        result = ev.run_rubric_judge(
            "rubric",
            tmp_path,
            self._CONFIG,
            _SCHEMA_DIR,
            isolation_launch=launch,
        )

        assert result.passed is True
        assert captured["command"][:3] == ["docker", "run", "judge"]
        assert captured["kwargs"]["cleanup_args"] == ["docker", "rm", "-f", "judge"]
        assert "ANTHROPIC_API_KEY" not in captured["kwargs"]["env"]
        assert not any("sk-" in part for part in captured["command"])


class TestRubricJudgeUnknownBackend:
    def test_unknown_judge_tool_fails_closed(self, tmp_path: Path) -> None:
        config = {"judge": {"tool": "not-a-real-backend"}}
        result = ev.run_rubric_judge(
            "rubric", tmp_path, config, _SCHEMA_DIR, runner=lambda *a, **k: None
        )
        assert result.passed is False
        assert result.error is True
