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


def _maker_infra_failure_result(agent: str = "backend-python-dev") -> dict:
    return {"maker": {"agent": agent, "tool": "codex"}, "infrastructure_failure": True}


def test_apply_action_effect_run_maker_infra_failure_retries_below_max() -> None:
    """I5 (PR #210 review round 5): a `run_maker` completion with `infrastructure_failure: True`
    (Maker timeout / non-zero `claude -p` exit, see `loop_driver._run_maker`) must increment
    the phase's infra-retry counter via `evaluate_guards()` -- previously this counter
    (`GuardCounters.infrastructure_failure_count`) was only ever reachable through
    `_apply_checker_result` (RUN_CHECKER/WAIT_EXTERNAL_REVIEW), so a Maker infra failure was
    unconditionally completed as `status="running"` regardless of how many consecutive
    failures occurred."""
    state = _state()
    counters = state.guards["implementation"]
    assert counters.infrastructure_failure_count == 0

    lc.apply_action_effect(state, lc.Action.RUN_MAKER.value, _maker_infra_failure_result(), None)

    assert state.status == "running"
    assert counters.infrastructure_failure_count == 1


def test_apply_action_effect_run_maker_infra_failure_fails_loop_once_exhausted() -> None:
    """I5: once the infra-retry counter reaches `guards.infrastructure_failure.max_retries`
    (3 by `DEFAULT_CONFIG`), a further Maker infra failure must convert the loop into a real
    failure (`on_failure.disposition`, `exit_failure` by default) instead of retrying forever."""
    state = _state()

    for _ in range(3):
        lc.apply_action_effect(
            state, lc.Action.RUN_MAKER.value, _maker_infra_failure_result(), None
        )

    assert state.status == "failed"
    assert state.stop_reason == "infrastructure_failure_exhausted"


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


def test_run_mechanical_command_pins_umask_regardless_of_caller_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Also covers the `LOOP_MECHANICAL_UMASK` unset case (default stays 0o022)."""
    monkeypatch.delenv(lc.MECHANICAL_UMASK_ENV, raising=False)
    original_umask = os.umask(0o077)
    try:
        output, exit_code = lc._run_mechanical_command("umask", str(tmp_path), 5)
        assert exit_code == 0
        assert int(output.strip(), 8) == 0o022
    finally:
        os.umask(original_umask)


def test_run_mechanical_command_env_override_opts_out_of_default_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LOOP_MECHANICAL_UMASK` lets operators restore a stricter umask (Issue #301 opt-out)."""
    monkeypatch.setenv(lc.MECHANICAL_UMASK_ENV, "077")
    output, exit_code = lc._run_mechanical_command("umask", str(tmp_path), 5)
    assert exit_code == 0
    assert int(output.strip(), 8) == 0o077


def test_run_mechanical_command_env_invalid_value_falls_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unparsable `LOOP_MECHANICAL_UMASK` falls back to the default rather than raising."""
    monkeypatch.setenv(lc.MECHANICAL_UMASK_ENV, "zzz")
    output, exit_code = lc._run_mechanical_command("umask", str(tmp_path), 5)
    assert exit_code == 0
    assert int(output.strip(), 8) == 0o022


def test_run_mechanical_command_explicit_env_scoped_umask_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit `env` mapping's own `LOOP_MECHANICAL_UMASK` is honored (Issue #301 review)."""
    monkeypatch.delenv(lc.MECHANICAL_UMASK_ENV, raising=False)
    explicit_env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        lc.MECHANICAL_UMASK_ENV: "077",
    }
    output, exit_code = lc._run_mechanical_command("umask", str(tmp_path), 5, env=explicit_env)
    assert exit_code == 0
    assert int(output.strip(), 8) == 0o077


def test_run_mechanical_command_explicit_env_does_not_fall_back_to_ambient_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit `env` lacking the key must not resurrect it from ambient `os.environ`."""
    monkeypatch.setenv(lc.MECHANICAL_UMASK_ENV, "077")
    explicit_env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    output, exit_code = lc._run_mechanical_command("umask", str(tmp_path), 5, env=explicit_env)
    assert exit_code == 0
    assert int(output.strip(), 8) == 0o022


def test_run_mechanical_command_on_start_receives_pid_then_none(tmp_path: Path) -> None:
    child_pids: list[int | None] = []

    output, exit_code = lc._run_mechanical_command(
        "printf ok", str(tmp_path), 5, on_start=child_pids.append
    )

    assert output == "ok"
    assert exit_code == 0
    assert len(child_pids) == 2
    assert isinstance(child_pids[0], int)
    assert child_pids[0] > 0
    assert child_pids[1] is None


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

    def spy(
        command: str,
        cwd: str,
        timeout_seconds: int,
        env: object = None,
        on_start: object = None,
    ) -> tuple[str, int]:
        captured["env"] = env
        return original(command, cwd, timeout_seconds, env=env, on_start=on_start)

    monkeypatch.setattr(lc, "_run_mechanical_command", spy)
    isolated_env = {"PATH": "/usr/bin:/bin"}
    lc.run_mechanical_checks(["printf ok"], str(tmp_path), 5, env=isolated_env)
    assert captured["env"] == isolated_env


def test_run_mechanical_checks_forwards_on_start_to_mechanical_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeDetector:
        @staticmethod
        def analyze(_tool_name: str, _tool_input: dict, _tool_response: dict) -> None:
            return None

    monkeypatch.setattr(lc, "_load_failure_detector", lambda: FakeDetector)
    child_pids: list[int | None] = []

    failures = lc.run_mechanical_checks(["printf ok"], str(tmp_path), 5, on_start=child_pids.append)

    assert failures == []
    assert len(child_pids) == 2
    assert isinstance(child_pids[0], int)
    assert child_pids[0] > 0
    assert child_pids[1] is None


# --------------------------------------------------------------------------------------------
# run_mechanical_checks: per-command wall-clock budget recomputation (Issue #219 P2-2)
# --------------------------------------------------------------------------------------------


def test_run_mechanical_checks_recomputes_timeout_per_command_from_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `remaining_budget`, every command reuses the same fixed `timeout_seconds` cap
    regardless of how much budget earlier commands in this same call already spent -- N
    commands can then collectively overshoot the caller's wall-clock deadline by up to N times.
    Passing `remaining_budget` must cap each command's own timeout to
    `min(timeout_seconds, remaining_budget())`, recomputed immediately before each command."""

    class FakeDetector:
        @staticmethod
        def analyze(_tool_name: str, _tool_input: dict, _tool_response: dict) -> None:
            return None

    monkeypatch.setattr(lc, "_load_failure_detector", lambda: FakeDetector)
    captured_timeouts: list[int] = []
    original = lc._run_mechanical_command

    def spy(
        command: str,
        cwd: str,
        timeout_seconds: int,
        env: object = None,
        on_start: object = None,
    ) -> tuple[str, int]:
        captured_timeouts.append(timeout_seconds)
        return original(command, cwd, timeout_seconds, env=env, on_start=on_start)

    monkeypatch.setattr(lc, "_run_mechanical_command", spy)
    # Simulate a shrinking wall-clock budget: 100s remaining before the 1st command, 3s before
    # the 2nd (as if the 1st command consumed most of the budget), which must cap the 2nd
    # command's own timeout down from the fixed 100s cap.
    remaining_values = iter([100.0, 3.0])

    failures = lc.run_mechanical_checks(
        ["printf first", "printf second"],
        str(tmp_path),
        100,
        remaining_budget=lambda: next(remaining_values),
    )

    assert failures == []
    assert captured_timeouts == [100, 3]


def test_run_mechanical_checks_skips_command_without_spawning_when_budget_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the remaining budget hits (or drops below) zero, no further command may spawn a
    subprocess at all -- it must be recorded as a synthetic timeout instead."""
    classified: list[tuple[str, dict]] = []

    class FakeDetector:
        @staticmethod
        def analyze(_tool_name: str, tool_input: dict, tool_response: dict) -> dict | None:
            classified.append((tool_input["command"], tool_response))
            if tool_response["exit_code"] == 124:
                return {
                    "failure_type": "infrastructure_failure",
                    "error_type": "timeout",
                    "detected_by": "exit_code",
                    "command_kind": "test",
                }
            return None

    monkeypatch.setattr(lc, "_load_failure_detector", lambda: FakeDetector)

    def _boom(*_a: object, **_k: object) -> tuple[str, int]:
        raise AssertionError("must not spawn a subprocess once the wall-clock budget is gone")

    monkeypatch.setattr(lc, "_run_mechanical_command", _boom)

    failures = lc.run_mechanical_checks(
        ["printf should-be-skipped"],
        str(tmp_path),
        100,
        remaining_budget=lambda: 0.0,
    )

    assert len(failures) == 1
    assert failures[0].failure_type == "infrastructure_failure"
    assert failures[0].error_type == "timeout"
    assert classified == [
        ("printf should-be-skipped", {"exit_code": 124, "stdout": classified[0][1]["stdout"]})
    ]


def test_run_mechanical_checks_without_remaining_budget_keeps_fixed_timeout_per_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backward compatibility: omitting `remaining_budget` (LP-1's `loop_step.py` call sites)
    must preserve the exact previous behavior of a fixed timeout for every command."""

    class FakeDetector:
        @staticmethod
        def analyze(_tool_name: str, _tool_input: dict, _tool_response: dict) -> None:
            return None

    monkeypatch.setattr(lc, "_load_failure_detector", lambda: FakeDetector)
    captured_timeouts: list[int] = []
    original = lc._run_mechanical_command

    def spy(
        command: str,
        cwd: str,
        timeout_seconds: int,
        env: object = None,
        on_start: object = None,
    ) -> tuple[str, int]:
        captured_timeouts.append(timeout_seconds)
        return original(command, cwd, timeout_seconds, env=env, on_start=on_start)

    monkeypatch.setattr(lc, "_run_mechanical_command", spy)

    lc.run_mechanical_checks(["printf first", "printf second"], str(tmp_path), 42)

    assert captured_timeouts == [42, 42]


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


def test_pr_review_new_and_partial_signature_changes_are_progress_and_reset_streak() -> None:
    """issue #213/A: `{A,B} -> {A,C}` (a completely different second finding) and, after
    another stall, `{A,C} -> {A}` (dropping one of two findings) both count as progress and
    reset an already-nonzero streak -- not just "start at zero", but an actual reset after a
    real stall was recorded."""
    state = lc._initial_state(
        "loop-np-3", "issue-loop", "hash", "/tmp/wt", "loop/issue-1", "pr_review_response"
    )
    config = {"guards": {"max_iterations": 10, "no_progress": {"repeat": 2}}}

    stall_ab = _pr_review_phase_check(
        lc.IterationFindings(frozenset({"sig-a", "sig-b"}), 0),
        lc.IterationFindings(frozenset({"sig-a", "sig-b"}), 2),
        "sig-stall-ab",
    )
    swap_to_ac = _pr_review_phase_check(
        lc.IterationFindings(frozenset({"sig-a", "sig-c"}), 1),
        lc.IterationFindings(frozenset({"sig-a", "sig-b"}), 0),
        "sig-swap-ac",
    )
    stall_ac = _pr_review_phase_check(
        lc.IterationFindings(frozenset({"sig-a", "sig-c"}), 0),
        lc.IterationFindings(frozenset({"sig-a", "sig-c"}), 1),
        "sig-stall-ac",
    )
    resolve_to_a = _pr_review_phase_check(
        lc.IterationFindings(frozenset({"sig-a"}), 1),
        lc.IterationFindings(frozenset({"sig-a", "sig-c"}), 0),
        "sig-resolve-a",
    )

    lc.evaluate_guards(state, stall_ab, None, config)
    assert state.guards["pr_review_response"].no_progress_streak == 1

    lc.evaluate_guards(state, swap_to_ac, None, config)
    assert state.guards["pr_review_response"].no_progress_streak == 0

    lc.evaluate_guards(state, stall_ac, None, config)
    assert state.guards["pr_review_response"].no_progress_streak == 1

    final = lc.evaluate_guards(state, resolve_to_a, None, config)
    assert final.reason != "no_progress"
    assert state.guards["pr_review_response"].no_progress_streak == 0


def test_pr_review_always_new_signature_never_stalls_but_hits_max_iterations() -> None:
    """issue #213/A: a Maker that keeps introducing genuinely new blocking findings every
    round is never stopped by the no-progress guard (each round's signature set differs from
    the last), only by `max_iterations` -- runaway iteration without convergence is bounded
    by that separate guard, exactly as designed."""
    state = lc._initial_state(
        "loop-np-4", "issue-loop", "hash", "/tmp/wt", "loop/issue-1", "pr_review_response"
    )
    config = {"guards": {"max_iterations": 3, "no_progress": {"repeat": 2}}}
    phase_def = {"on_failure": {"disposition": lc.Action.EXIT_FAILURE.value}}

    round1 = _pr_review_phase_check(
        lc.IterationFindings(frozenset({"sig-1"}), 1), lc.IterationFindings(frozenset(), 0), "s1"
    )
    round2 = _pr_review_phase_check(
        lc.IterationFindings(frozenset({"sig-2"}), 1),
        lc.IterationFindings(frozenset({"sig-1"}), 1),
        "s2",
    )
    round3 = _pr_review_phase_check(
        lc.IterationFindings(frozenset({"sig-3"}), 1),
        lc.IterationFindings(frozenset({"sig-2"}), 1),
        "s3",
    )

    first = lc.evaluate_guards(state, round1, phase_def, config)
    second = lc.evaluate_guards(state, round2, phase_def, config)
    third = lc.evaluate_guards(state, round3, phase_def, config)

    assert first.disposition == "continue"
    assert second.disposition == "continue"
    assert state.guards["pr_review_response"].no_progress_streak == 0
    assert third.disposition == lc.Action.EXIT_FAILURE.value
    assert third.reason == "max_iterations"
