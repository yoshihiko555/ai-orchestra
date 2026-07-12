"""Guard, signature, and redaction tests for loop_common."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.module_loader import load_module

lc = load_module("loop_common_guards", "packages/loop-harness/lib/loop_common.py")
ld = load_module("loop_definition_for_guards", "packages/loop-harness/lib/loop_definition.py")


def _state() -> object:
    return lc.LoopState(
        schema_version=1,
        loop_id="loop-1",
        definition_id="issue-loop",
        repo_identity_hash="abcd1234",
        phase="implementation",
        iteration=0,
        status="running",
        worktree_path="/tmp/wt",
        branch="loop/issue-1",
        pr_number=None,
        guards={"implementation": lc.GuardCounters()},
        last_check_result=None,
        pending_action=None,
        last_completed_action=None,
        stop_reason=None,
        pr_review=None,
        ignored_untrusted_comment_count=0,
        created_at=lc.now_iso(),
        updated_at=lc.now_iso(),
        state_version=0,
    )


def _phase_check(passed: bool = False, signature: str = "sig", infra: bool = False) -> object:
    return lc.PhaseCheckResult(
        passed=passed,
        results=[],
        signature=signature,
        infrastructure_failure=infra,
    )


def test_evaluate_guards_infrastructure_failure_is_first_and_not_stopped() -> None:
    state = _state()
    config = {"guards": {"infrastructure_failure": {"max_retries": 1}}}
    decision = lc.evaluate_guards(state, _phase_check(passed=True, infra=True), None, config)
    assert decision.disposition == lc.Action.EXIT_FAILURE.value
    assert decision.reason == "infrastructure_failure_exhausted"
    assert state.status == "running"


def test_external_reviewer_unavailable_stops_without_changing_guard_counters() -> None:
    state = _state()
    counters = state.guards["implementation"]
    counters.iteration = 2
    counters.no_progress_streak = 1
    counters.last_signature = "previous"
    counters.infrastructure_failure_count = 1
    phase_check = lc.PhaseCheckResult(
        passed=False,
        results=[],
        signature="external_reviewer_unavailable",
        infrastructure_failure=False,
        metadata={"reviewer_unavailable_reason": "rate_limited"},
    )

    decision = lc.evaluate_guards(state, phase_check, None, {})

    assert decision == lc.GuardDecision(lc.Action.STOP.value, "external_reviewer_unavailable")
    assert counters == lc.GuardCounters(
        iteration=2,
        no_progress_streak=1,
        last_signature="previous",
        infrastructure_failure_count=1,
    )


def test_evaluate_guards_pass_before_no_progress_and_iteration_limit() -> None:
    state = _state()
    counters = state.guards["implementation"]
    counters.iteration = 99
    counters.no_progress_streak = 99
    decision = lc.evaluate_guards(state, _phase_check(passed=True, signature="same"), None, {})
    assert decision.disposition == lc.Action.EXIT_SUCCESS.value
    assert counters.no_progress_streak == 0
    assert counters.last_signature is None


def test_evaluate_guards_no_progress_precedes_iteration_limit() -> None:
    state = _state()
    counters = state.guards["implementation"]
    counters.iteration = 99
    counters.last_signature = "same"
    counters.no_progress_streak = 1
    decision = lc.evaluate_guards(state, _phase_check(signature="same"), None, {})
    assert decision.reason == "no_progress"


def test_evaluate_guards_iteration_limit_uses_defaults() -> None:
    state = _state()
    lc.evaluate_guards(state, _phase_check(signature="a"), None, {})
    lc.evaluate_guards(state, _phase_check(signature="b"), None, {})
    decision = lc.evaluate_guards(state, _phase_check(signature="c"), None, {})
    assert decision.reason == "max_iterations"


def test_evaluate_guards_uses_phase_definition_guard_overrides() -> None:
    phase_def = ld.PhaseDefinition(
        name="implementation",
        maker={"agent": "backend-python-dev"},
        checker={"mechanical": []},
        guards={
            "max_iterations": 5,
            "no_progress": {"signature": "implementation", "repeat": 3},
        },
        on_success={"disposition": lc.Action.EXIT_SUCCESS.value},
        on_failure={"disposition": lc.Action.EXIT_FAILURE.value},
    )
    config = {
        "guards": {
            "max_iterations": 1,
            "no_progress": {"repeat": 1},
            "infrastructure_failure": {"max_retries": 3},
        }
    }
    state = _state()

    first = lc.evaluate_guards(state, _phase_check(signature="same"), phase_def, config)
    second = lc.evaluate_guards(state, _phase_check(signature="same"), phase_def, config)
    third = lc.evaluate_guards(state, _phase_check(signature="same"), phase_def, config)

    assert first.disposition == "continue"
    assert second.disposition == "continue"
    assert third.disposition == lc.Action.EXIT_FAILURE.value
    assert third.reason == "no_progress"


def test_combine_check_results_missing_required_mechanical_is_infra_failure() -> None:
    llm = lc.CheckResult(True, "llm_review", None, [], "llm.json")
    result = lc.combine_check_results([llm], {"critical": 0, "high": 0}, frozenset({"mechanical"}))
    assert result.passed is False
    assert result.infrastructure_failure is True


def test_combine_check_results_missing_required_llm_is_infra_failure() -> None:
    mech = lc.CheckResult(True, "mechanical", "", [], "mechanical.log")
    result = lc.combine_check_results(
        [mech], {"critical": 0, "high": 0}, frozenset({"mechanical", "llm_review"})
    )
    assert result.passed is False
    assert result.infrastructure_failure is True


def test_llm_review_only_failure_uses_finding_signature() -> None:
    mech = lc.CheckResult(True, "mechanical", "", [], "mechanical.log")
    first = lc.CheckResult(
        True,
        "llm_review",
        None,
        [lc.Finding("high", "Use constant-time token compare", "security-reviewer", "auth.py", 12)],
        "review.json",
    )
    second = lc.CheckResult(
        True,
        "llm_review",
        None,
        [lc.Finding("high", "Validate redirect URI", "security-reviewer", "auth.py", 12)],
        "review.json",
    )
    r1 = lc.combine_check_results(
        [mech, first], {"critical": 0, "high": 0}, frozenset({"mechanical", "llm_review"})
    )
    r2 = lc.combine_check_results(
        [mech, second], {"critical": 0, "high": 0}, frozenset({"mechanical", "llm_review"})
    )
    assert r1.signature
    assert r1.signature != r2.signature


def test_extract_failed_test_ids_and_fallback_signature() -> None:
    output = "FAILED tests/test_a.py::test_one - AssertionError\nFAILED tests/test_b.py::test_two\n"
    assert lc.extract_failed_test_ids(output) == [
        "tests/test_a.py::test_one",
        "tests/test_b.py::test_two",
    ]
    a = lc.compute_implementation_signature(
        [lc.MechanicalFailure("pytest -q", "test_failure", "syntax", "tmp/a.py:10: x")]
    )
    b = lc.compute_implementation_signature(
        [lc.MechanicalFailure("pytest -q", "test_failure", "syntax", "tmp/a.py:99: x")]
    )
    assert a == b


def test_extract_lint_rule_ids() -> None:
    output = "pkg/a.py:10:5: F401 unused import\npkg/b.py:2:1: E501 line too long\n"
    assert lc.extract_lint_rule_ids(output) == ["E501", "F401"]


def test_lint_signature_falls_back_to_normalized_excerpt() -> None:
    first = lc.compute_implementation_signature(
        [lc.MechanicalFailure("ruff check .", "lint_failure", "unknown", "tmp/a.py:10: bad")]
    )
    second = lc.compute_implementation_signature(
        [lc.MechanicalFailure("ruff check .", "lint_failure", "unknown", "tmp/a.py:99: bad")]
    )
    assert first == second


def test_run_mechanical_checks_invokes_failure_detector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeDetector:
        @staticmethod
        def analyze(_tool_name: str, tool_input: dict, tool_response: dict) -> dict | None:
            if tool_response["exit_code"] == 0:
                return None
            return {
                "failure_type": "test_failure",
                "error_type": "assertion",
                "detected_by": "exit_code",
                "command_kind": "test",
            }

    monkeypatch.setattr(lc, "_load_failure_detector", lambda: FakeDetector)
    failures = lc.run_mechanical_checks(
        [
            "printf ok",
            "printf 'FAILED tests/test_a.py::test_one - AssertionError'; exit 1",
        ],
        str(tmp_path),
        5,
    )
    assert len(failures) == 1
    assert failures[0].command.startswith("printf 'FAILED")
    assert failures[0].failure_type == "test_failure"


def test_run_mechanical_checks_heartbeats_after_each_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeDetector:
        @staticmethod
        def analyze(_tool_name: str, _tool_input: dict, _tool_response: dict) -> None:
            return None

    heartbeats: list[str] = []
    monkeypatch.setattr(lc, "_load_failure_detector", lambda: FakeDetector)

    failures = lc.run_mechanical_checks(
        ["printf first", "printf second"],
        str(tmp_path),
        5,
        heartbeat=lambda: heartbeats.append("beat"),
    )

    assert failures == []
    assert heartbeats == ["beat", "beat"]


def test_run_mechanical_checks_persists_output_when_analyzer_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingDetector:
        @staticmethod
        def analyze(_tool_name: str, _tool_input: dict, _tool_response: dict) -> None:
            raise RuntimeError("analyzer failed")

    events: list[object] = []
    monkeypatch.setattr(lc, "_load_failure_detector", lambda: FailingDetector)

    with pytest.raises(RuntimeError, match="analyzer failed"):
        lc.run_mechanical_checks(
            ["printf captured-output"],
            str(tmp_path),
            5,
            heartbeat=lambda: events.append("heartbeat"),
            artifact_writer=lambda index, command, output, exit_code: events.append(
                (index, command, output, exit_code)
            ),
        )

    assert events == [
        "heartbeat",
        (1, "printf captured-output", "captured-output", 0),
    ]


def test_run_mechanical_command_env_none_inherits_os_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-C1 backward compatibility: omitting `env` keeps inheriting the real process env."""
    monkeypatch.setenv("LOOP_HARNESS_ENV_PROBE", "inherited-value")
    output, exit_code = lc._run_mechanical_command(
        'printf "%s" "$LOOP_HARNESS_ENV_PROBE"', str(tmp_path), 5
    )
    assert exit_code == 0
    assert output == "inherited-value"


def test_run_mechanical_command_env_stripped_key_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-C1: an explicit isolated `env` dict is what the child subprocess actually sees."""
    monkeypatch.setenv("LOOP_HARNESS_ENV_PROBE", "should-not-be-visible")
    stripped_env = {
        k: v for k, v in __import__("os").environ.items() if k != "LOOP_HARNESS_ENV_PROBE"
    }
    output, exit_code = lc._run_mechanical_command(
        'printf "%s" "${LOOP_HARNESS_ENV_PROBE:-absent}"', str(tmp_path), 5, env=stripped_env
    )
    assert exit_code == 0
    assert output == "absent"


def test_run_mechanical_command_times_out_with_exit_124(tmp_path: Path) -> None:
    output, exit_code = lc._run_mechanical_command("sleep 5", str(tmp_path), 0.2)
    assert exit_code == 124
    assert "command timed out" in output


def test_run_mechanical_command_kills_grandchildren_on_timeout(tmp_path: Path) -> None:
    """Code review #16: timeout must reap the whole process group, not just direct `bash`.

    Without process-group kill, only the direct `bash -lc ...` child is killed and the
    background `sleep` it spawned survives past the timeout.
    """
    pid_file = tmp_path / "child.pid"
    command = f"sleep 5 & echo $! > {pid_file}; wait"
    output, exit_code = lc._run_mechanical_command(command, str(tmp_path), 0.2)
    assert exit_code == 124
    assert "command timed out" in output
    child_pid = int(pid_file.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_run_mechanical_checks_forwards_env_to_mechanical_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-C1: `run_mechanical_checks()` threads its `env` kwarg through to each command."""

    class FakeDetector:
        @staticmethod
        def analyze(_tool_name: str, _tool_input: dict, _tool_response: dict) -> None:
            return None

    monkeypatch.setattr(lc, "_load_failure_detector", lambda: FakeDetector)
    captured: dict[str, object] = {}
    original = lc._run_mechanical_command

    def spy(command: str, cwd: str, timeout_seconds: int, env: object = None) -> tuple[str, int]:
        captured["env"] = env
        return original(command, cwd, timeout_seconds, env=env)

    monkeypatch.setattr(lc, "_run_mechanical_command", spy)
    isolated_env = {"PATH": "/usr/bin:/bin"}
    lc.run_mechanical_checks(["printf ok"], str(tmp_path), 5, env=isolated_env)
    assert captured["env"] == isolated_env


def test_redact_payload_and_audit_payload_shape() -> None:
    state = _state()
    payload = lc.build_audit_payload(
        "loop_iteration",
        state,
        action_id="act-1",
        maker={"agent": "backend-python-dev", "token": "Bearer abc.def"},
        checker={"llm_review": {"agent": "code-reviewer"}},
    )
    assert payload["maker"]["agent"] == "backend-python-dev"
    assert payload["checker"]["llm_review"]["agent"] == "code-reviewer"
    assert payload["maker"]["token"] == "[REDACTED]"


def test_redact_masks_full_multiline_pem_block() -> None:
    text = (
        "before\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAfakebase64line\n"
        "anotherfakebase64line\n"
        "-----END RSA PRIVATE KEY-----\n"
        "after"
    )

    redacted = lc.redact(text)

    assert redacted == "before\n[REDACTED]\nafter"
    assert "PRIVATE KEY" not in redacted
    assert "fakebase64line" not in redacted


def test_redact_masks_multi_word_values_until_field_boundary() -> None:
    assert lc.redact("password: my secret phrase") == "[REDACTED]"

    redacted = lc.redact("token: abc123, next_field: keep_me")

    assert redacted == "[REDACTED], next_field: keep_me"
    assert "abc123" not in redacted


def test_redact_payload_masks_sensitive_dict_keys_recursively() -> None:
    payload = {
        "maker": {"api_key": "xyz-no-prefix", "name": "backend-python-dev"},
        "checker": {"nested": {"secret": "plain-value", "agent": "code-reviewer"}},
    }

    redacted = lc.redact_payload(payload)

    assert redacted["maker"]["api_key"] == "[REDACTED]"
    assert redacted["maker"]["name"] == "backend-python-dev"
    assert redacted["checker"]["nested"]["secret"] == "[REDACTED]"
    assert redacted["checker"]["nested"]["agent"] == "code-reviewer"


def _pr_review_phase_check(
    current: lc.IterationFindings, previous: lc.IterationFindings, signature: str
) -> lc.PhaseCheckResult:
    return lc.PhaseCheckResult(
        passed=False,
        results=[],
        signature=signature,
        infrastructure_failure=False,
        metadata={
            "current_iteration_findings": current,
            "previous_iteration_findings": previous,
        },
    )


def test_pr_review_progress_resets_streak_so_single_stall_continues() -> None:
    state = lc._initial_state(
        "loop-np-1", "issue-loop", "hash", "/tmp/wt", "loop/issue-1", "pr_review_response"
    )
    config = {"guards": {"max_iterations": 10, "no_progress": {"repeat": 2}}}
    progress = _pr_review_phase_check(
        lc.IterationFindings(frozenset({"sig-x"}), 1),
        lc.IterationFindings(frozenset({"sig-y"}), 2),
        "sig-progress",
    )
    single_stall = _pr_review_phase_check(
        lc.IterationFindings(frozenset({"sig-x"}), 0),
        lc.IterationFindings(frozenset({"sig-x"}), 1),
        "sig-stall-1",
    )

    first = lc.evaluate_guards(state, progress, None, config)
    second = lc.evaluate_guards(state, single_stall, None, config)

    assert first.reason != "no_progress"
    assert second.reason != "no_progress"
    assert state.guards["pr_review_response"].no_progress_streak == 1


def test_pr_review_two_consecutive_stalls_fail_with_no_progress() -> None:
    state = lc._initial_state(
        "loop-np-2", "issue-loop", "hash", "/tmp/wt", "loop/issue-1", "pr_review_response"
    )
    config = {"guards": {"max_iterations": 10, "no_progress": {"repeat": 2}}}
    progress = _pr_review_phase_check(
        lc.IterationFindings(frozenset({"sig-x"}), 1),
        lc.IterationFindings(frozenset({"sig-y"}), 2),
        "sig-progress",
    )
    stall_one = _pr_review_phase_check(
        lc.IterationFindings(frozenset({"sig-x"}), 0),
        lc.IterationFindings(frozenset({"sig-x"}), 1),
        "sig-stall-1",
    )
    stall_two = _pr_review_phase_check(
        lc.IterationFindings(frozenset({"sig-x"}), 0),
        lc.IterationFindings(frozenset({"sig-x"}), 0),
        "sig-stall-2",
    )

    lc.evaluate_guards(state, progress, None, config)
    lc.evaluate_guards(state, stall_one, None, config)
    final = lc.evaluate_guards(state, stall_two, None, config)

    assert final.reason == "no_progress"
