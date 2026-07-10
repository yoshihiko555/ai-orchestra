"""CLI capability gate のテスト（EV-27, Sec2-7）。

subprocess はすべてフェイク runner に差し替え、実 `claude`/`codex` を一切呼ばない。
"""

from __future__ import annotations

import subprocess

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common",
    "packages/meta-harness/lib/meta_harness_common.py",
)
ev = load_module(
    "meta_harness_evaluator_capability_gate",
    "packages/meta-harness/lib/evaluator.py",
)


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _always_ok_runner(*args, **kwargs) -> subprocess.CompletedProcess:
    if args and args[0] and args[0][0] == "claude" and args[0][1] == "--version":
        return _completed(0, stdout="2.1.202 (Claude Code)")
    # max_budget_usd smoke check now validates flag acceptance via a `type: result`
    # JSON payload rather than exit code (real `claude` exits non-zero even when the
    # flag is accepted, because the deliberately tiny smoke budget gets exceeded).
    return _completed(0, stdout='{"type": "result"}')


class TestCapabilityGateHappyPath:
    def test_ok_when_all_checks_pass(self, monkeypatch) -> None:
        monkeypatch.setattr(ev.shutil, "which", lambda name: f"/usr/bin/{name}")
        config = {"evaluate": {"cli_version_pin": None}, "judge": {"tool": "codex"}}
        caps = ev.check_cli_capabilities(config, runner=_always_ok_runner)
        assert caps.ok is True
        assert caps.reason is None
        assert caps.claude_version == "2.1.202 (Claude Code)"

    def test_version_pin_match_when_equal(self, monkeypatch) -> None:
        monkeypatch.setattr(ev.shutil, "which", lambda name: f"/usr/bin/{name}")
        config = {
            "evaluate": {"cli_version_pin": "2.1.202 (Claude Code)"},
            "judge": {"tool": "codex"},
        }
        caps = ev.check_cli_capabilities(config, runner=_always_ok_runner)
        assert caps.ok is True
        assert caps.version_pin_match is True


class TestCapabilityGateVersionPinMismatch:
    def test_version_pin_mismatch_fails_gate(self) -> None:
        config = {"evaluate": {"cli_version_pin": "9.9.9"}, "judge": {"tool": "codex"}}
        caps = ev.check_cli_capabilities(config, runner=_always_ok_runner)
        assert caps.ok is False
        assert caps.version_pin_match is False
        assert "mismatch" in caps.reason

    def test_version_pin_none_skips_match_check(self) -> None:
        config = {"evaluate": {"cli_version_pin": None}, "judge": {"tool": "codex"}}
        caps = ev.check_cli_capabilities(config, runner=_always_ok_runner)
        assert caps.version_pin_match is None


class TestCapabilityGateFlagRejection:
    def test_stream_json_rejected_fails_gate(self) -> None:
        def runner(cmd, **kwargs):
            if cmd[0] == "claude" and cmd[1] == "--version":
                return _completed(0, stdout="2.1.202")
            if "--output-format" in cmd and "stream-json" in cmd:
                return _completed(1, stderr="unknown flag")
            return _completed(0)

        config = {"evaluate": {"cli_version_pin": None}, "judge": {"tool": "codex"}}
        caps = ev.check_cli_capabilities(config, runner=runner)
        assert caps.ok is False
        assert caps.checks["stream_json"] is False
        assert "stream_json" in caps.reason

    def test_max_budget_usd_rejected_fails_gate(self) -> None:
        def runner(cmd, **kwargs):
            if cmd[0] == "claude" and cmd[1] == "--version":
                return _completed(0, stdout="2.1.202")
            if "--max-budget-usd" in cmd:
                return _completed(1, stderr="unknown flag")
            return _completed(0)

        config = {"evaluate": {"cli_version_pin": None}, "judge": {"tool": "codex"}}
        caps = ev.check_cli_capabilities(config, runner=runner)
        assert caps.ok is False
        assert caps.checks["max_budget_usd"] is False

    def test_max_budget_usd_exceeded_but_accepted_passes_gate(self) -> None:
        """実測: `--max-budget-usd 0.02` は極小のためフラグが有効でも予算超過で
        exit code が非ゼロになる（`error_max_budget_usd`）。これはフラグが CLI に
        認識され正しく機能した証拠であり、gate は通過させるべき（Sec2-7）。"""

        def runner(cmd, **kwargs):
            if cmd[0] == "claude" and cmd[1] == "--version":
                return _completed(0, stdout="2.1.202")
            if "--max-budget-usd" in cmd:
                return _completed(
                    1,
                    stdout='{"type":"result","subtype":"error_max_budget_usd"}',
                )
            return _completed(0, stdout='{"type": "result"}')

        config = {"evaluate": {"cli_version_pin": None}, "judge": {"tool": "codex"}}
        caps = ev.check_cli_capabilities(config, runner=runner)
        assert caps.checks["max_budget_usd"] is True

    def test_claude_version_unavailable_fails_gate(self) -> None:
        def runner(cmd, **kwargs):
            raise OSError("claude: command not found")

        config = {"evaluate": {"cli_version_pin": None}, "judge": {"tool": "codex"}}
        caps = ev.check_cli_capabilities(config, runner=runner)
        assert caps.ok is False
        assert caps.claude_version is None
        assert "could not determine" in caps.reason


class TestCapabilityGateJudgeBackendSpecific:
    def test_codex_missing_binary_fails_gate(self, monkeypatch) -> None:
        monkeypatch.setattr(ev.shutil, "which", lambda name: None)
        config = {"evaluate": {"cli_version_pin": None}, "judge": {"tool": "codex"}}
        caps = ev.check_cli_capabilities(config, runner=_always_ok_runner)
        assert caps.ok is False
        assert caps.checks["codex_exec_present"] is False

    def test_claude_bare_missing_api_key_fails_gate(self, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(ev, "_api_key_helper_configured", lambda: False)
        config = {"evaluate": {"cli_version_pin": None}, "judge": {"tool": "claude-bare"}}
        caps = ev.check_cli_capabilities(config, runner=_always_ok_runner)
        assert caps.ok is False
        assert caps.checks["bare_api_key_present"] is False

    def test_claude_bare_api_key_present_passes_that_check(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key-for-unit-test-only")
        config = {"evaluate": {"cli_version_pin": None}, "judge": {"tool": "claude-bare"}}
        caps = ev.check_cli_capabilities(config, runner=_always_ok_runner)
        assert caps.checks["bare_api_key_present"] is True

    def test_claude_bare_api_key_helper_configured_passes_that_check(self, monkeypatch) -> None:
        """`ANTHROPIC_API_KEY` が無くても `apiKeyHelper` 構成があれば通す（Sec14-1）。"""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(ev, "_api_key_helper_configured", lambda: True)
        config = {"evaluate": {"cli_version_pin": None}, "judge": {"tool": "claude-bare"}}
        caps = ev.check_cli_capabilities(config, runner=_always_ok_runner)
        assert caps.checks["bare_api_key_present"] is True


class TestCapabilityGateNoWorktreeOnFailure:
    """capability gate 失敗時、CLI は worktree を1つも作らずに exit すること（Sec2-7）。"""

    def test_cmd_evaluate_returns_validation_error_without_calling_evaluate_candidate(
        self, git_project, run_meta, default_overlay, tmp_path, monkeypatch
    ) -> None:
        cli = load_module(
            "meta_harness_cli_capability_gate_test",
            "packages/meta-harness/scripts/meta_harness.py",
        )
        run_meta("init", project=git_project, check=True)
        overlay_dir = default_overlay(tmp_path)
        register_result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            "--json",
            project=git_project,
            check=True,
        )
        import json as _json

        cand_id = _json.loads(register_result.stdout)["cand_id"]

        def fake_check_cli_capabilities(config, runner=None):
            return cli.ev.CliCapabilities(
                claude_version=None,
                version_pin=None,
                version_pin_match=None,
                checks={"stream_json": False},
                judge_tool="codex",
                ok=False,
                reason="forced failure for test",
            )

        def evaluate_candidate_must_not_be_called(**kwargs):
            raise AssertionError("evaluate_candidate must not be called when capability gate fails")

        monkeypatch.setattr(cli.ev, "check_cli_capabilities", fake_check_cli_capabilities)
        monkeypatch.setattr(cli.ev, "evaluate_candidate", evaluate_candidate_must_not_be_called)

        exit_code = cli.cmd_evaluate(str(git_project), cand_id, None, None, False)

        assert exit_code == cli.EXIT_VALIDATION_ERROR


class TestEvaluateCandidateExceptionNormalization:
    """CodeRabbit 指摘（meta_harness.py:558）: `load_scenario()` 由来の `OSError` /
    `yaml.YAMLError` も `ValueError` と同様に `EXIT_VALIDATION_ERROR` に正規化され、
    traceback を `main()` まで漏らさないこと。"""

    def _prepare_cli(self, git_project, run_meta, default_overlay, tmp_path, monkeypatch):
        cli = load_module(
            "meta_harness_cli_exception_normalization_test",
            "packages/meta-harness/scripts/meta_harness.py",
        )
        run_meta("init", project=git_project, check=True)
        overlay_dir = default_overlay(tmp_path)
        register_result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            "--json",
            project=git_project,
            check=True,
        )
        import json as _json

        cand_id = _json.loads(register_result.stdout)["cand_id"]

        def fake_check_cli_capabilities(config, runner=None):
            return cli.ev.CliCapabilities(
                claude_version="2.1.202",
                version_pin=None,
                version_pin_match=None,
                checks={},
                judge_tool="codex",
                ok=True,
                reason=None,
            )

        monkeypatch.setattr(cli.ev, "check_cli_capabilities", fake_check_cli_capabilities)
        return cli, cand_id

    def test_yaml_error_from_evaluate_candidate_exits_2_not_traceback(
        self, git_project, run_meta, default_overlay, tmp_path, monkeypatch
    ) -> None:
        cli, cand_id = self._prepare_cli(
            git_project, run_meta, default_overlay, tmp_path, monkeypatch
        )

        def raising_evaluate_candidate(**kwargs):
            raise cli.ev.yaml.YAMLError("forced malformed yaml for test")

        monkeypatch.setattr(cli.ev, "evaluate_candidate", raising_evaluate_candidate)
        exit_code = cli.cmd_evaluate(str(git_project), cand_id, None, None, False)
        assert exit_code == cli.EXIT_VALIDATION_ERROR

    def test_os_error_from_evaluate_candidate_exits_2_not_traceback(
        self, git_project, run_meta, default_overlay, tmp_path, monkeypatch
    ) -> None:
        cli, cand_id = self._prepare_cli(
            git_project, run_meta, default_overlay, tmp_path, monkeypatch
        )

        def raising_evaluate_candidate(**kwargs):
            raise OSError("forced I/O error for test")

        monkeypatch.setattr(cli.ev, "evaluate_candidate", raising_evaluate_candidate)
        exit_code = cli.cmd_evaluate(str(git_project), cand_id, None, None, False)
        assert exit_code == cli.EXIT_VALIDATION_ERROR
