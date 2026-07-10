"""CLI tests for loop_step.py."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.module_loader import REPO_ROOT, load_module

SCRIPT = REPO_ROOT / "packages" / "loop-harness" / "scripts" / "loop_step.py"

lc = load_module("loop_common_for_loop_step_tests", "packages/loop-harness/lib/loop_common.py")
loop_step = load_module("loop_step_cli_tests", "packages/loop-harness/scripts/loop_step.py")
event_logger = load_module(
    "event_logger_for_loop_step_tests", "packages/audit/hooks/event_logger.py"
)


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "loop-harness@example.com"], path)
    _git(["config", "user.name", "Loop Harness Test"], path)
    (path / "README.md").write_text("root\n", encoding="utf-8")
    _git(["add", "README.md"], path)
    _git(["commit", "-m", "init"], path)


def _run_cli(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def _payload(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert proc.stdout.count("\n") == 1
    data = json.loads(proc.stdout)
    assert isinstance(data, dict)
    return data


def _start(repo: Path, issue: int = 7) -> dict[str, Any]:
    proc = _run_cli(["start", "--issue", str(issue), "--project", str(repo)])
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    return _payload(proc)


def _propose(repo: Path, loop_id: str, lease_token: str) -> dict[str, Any]:
    proc = _run_cli(
        [
            "propose",
            "--loop-id",
            loop_id,
            "--lease-token",
            lease_token,
            "--project",
            str(repo),
        ]
    )
    assert proc.returncode == 0, proc.stderr
    return _payload(proc)


def _complete(
    repo: Path,
    loop_id: str,
    proposal: dict[str, Any],
    lease_token: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    proc = _run_cli(
        [
            "complete",
            "--loop-id",
            loop_id,
            "--action-id",
            proposal["action_id"],
            "--state-version",
            str(proposal["state_version"]),
            "--result",
            json.dumps(result or {}),
            "--lease-token",
            lease_token,
            "--project",
            str(repo),
        ]
    )
    assert proc.returncode == 0, proc.stderr
    return _payload(proc)


def _set_lock_heartbeat(repo: Path, loop_id: str, value: str) -> None:
    path = lc.lock_path(loop_id, str(repo))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["heartbeat_at"] = value
    path.write_text(json.dumps(data), encoding="utf-8")


def _old_live_heartbeat() -> str:
    return (datetime.now(UTC) - timedelta(seconds=60)).isoformat()


def _write_running_state(repo: Path, loop_id: str = "abcd1234-issue-1") -> lc.LockInfo:
    state = lc._initial_state(
        loop_id,
        "issue-loop",
        loop_step.wm.resolve_repo_identity_hash(str(repo)),
        str(repo),
        "main",
        "implementation",
    )
    state.status = "running"
    lc._write_state(state, str(repo))
    lock = lc.acquire_lock(loop_id, str(repo), "owner", 3600, host=socket.gethostname())
    assert lock is not None
    return lock


def _prepare_pending_checker(repo: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    _init_repo(repo)
    started = _start(repo)
    _complete(repo, started["loop_id"], started, started["lease_token"])
    checker = _propose(repo, started["loop_id"], started["lease_token"])
    assert checker["action"] == lc.Action.RUN_CHECKER.value
    return started, checker


def _write_llm_result(
    path: Path,
    *,
    findings: list[Any] | None = None,
    layer: str = "llm_review",
    infrastructure_failure: bool = False,
    passed: bool | None = None,
) -> Path:
    result = loop_step.lc.CheckResult(
        passed=not infrastructure_failure if passed is None else passed,
        layer=layer,
        signature=None,
        findings=findings or [],
        raw_artifact_path=str(path),
        infrastructure_failure=infrastructure_failure,
    )
    path.write_text(
        json.dumps(loop_step.lc.check_result_to_dict(result), ensure_ascii=False),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _run_checker_args(
    repo: Path,
    started: dict[str, Any],
    checker: dict[str, Any],
    llm_results: list[Path],
) -> list[str]:
    args = [
        "run-checker",
        "--loop-id",
        started["loop_id"],
        "--action-id",
        checker["action_id"],
        "--state-version",
        str(checker["state_version"]),
        "--lease-token",
        started["lease_token"],
    ]
    reviewers = ["code-reviewer", "security-reviewer"]
    for reviewer, path in zip(reviewers, llm_results):
        args.extend(["--llm-result", f"{reviewer}=@{path}"])
    return [*args, "--project", str(repo)]


def _override_implementation_commands(repo: Path, commands: list[str]) -> None:
    source = (
        REPO_ROOT / "packages" / "loop-harness" / "config" / "loops" / "issue-loop.yaml"
    ).read_text(encoding="utf-8")
    original = "          - pytest -q\n          - ruff check ."
    replacement = "\n".join(f"          - {command}" for command in commands)
    assert original in source
    destination = repo / ".claude" / "config" / "loop-harness" / "loops" / "issue-loop.yaml"
    destination.parent.mkdir(parents=True)
    destination.write_text(source.replace(original, replacement), encoding="utf-8")


def test_start_and_complete_emit_single_line_json(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    started = _start(repo)

    assert {
        "loop_id",
        "action",
        "action_id",
        "state_version",
        "phase",
        "iteration",
        "params",
        "reason",
        "lease_token",
    } <= started.keys()
    assert "expected_phase" not in started
    assert started["action"] == lc.Action.RUN_MAKER.value
    assert started["state_version"] == 1
    assert started["params"] == {
        "maker_agent": "auto",
        "prompt_template": "facets/instructions/loop-issue.md#maker",
        "worktree_path": lc.load_state(started["loop_id"], str(repo)).worktree_path,
        "branch": lc.load_state(started["loop_id"], str(repo)).branch,
        "issue_number": 7,
        "repo_identity_verified": True,
    }

    proc = _run_cli(
        [
            "complete",
            "--loop-id",
            started["loop_id"],
            "--action-id",
            started["action_id"],
            "--state-version",
            str(started["state_version"]),
            "--result",
            "{}",
            "--lease-token",
            started["lease_token"],
            "--project",
            str(repo),
        ]
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    completed = _payload(proc)
    assert completed == {
        "ok": True,
        "loop_id": started["loop_id"],
        "state_version": 2,
        "next": "call propose again",
        "idempotent_replay": False,
    }


def test_loop_audit_events_are_emitted_when_trace_state_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    event_logger.save_trace_state(
        "tid-loop",
        session_id="session-loop",
        expected_route="codex",
        project_dir=str(repo),
    )

    started = _start(repo)
    _complete(repo, started["loop_id"], started, started["lease_token"])
    state = lc.load_state(started["loop_id"], str(repo))
    state.status = "stopped"
    state.stop_reason = "safety_stop"
    state.pending_action = None
    lc._write_state(state, str(repo))
    _propose(repo, started["loop_id"], started["lease_token"])

    log_path = Path(event_logger.get_session_log_path("session-loop", str(repo)))
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    event_types = [event["type"] for event in events]
    assert "loop_start" in event_types
    assert "loop_iteration" in event_types
    assert "loop_stop" in event_types
    loop_stop = next(event for event in events if event["type"] == "loop_stop")
    assert loop_stop["data"]["final_status"] == "stopped"
    assert loop_stop["data"]["stop_reason"] == "safety_stop"


def test_loop_iteration_audit_preserves_advance_phase_result(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    pending_state = lc.load_state("abcd1234-issue-1", str(repo))
    pending_state.pending_action = lc.PendingAction(
        "act-advance", lc.Action.ADVANCE_PHASE.value, "implementation", 1, lc.now_iso()
    )
    pending_state.last_check_result = {"next_phase": "pr_review_response"}
    pending_state.state_version = 1
    lc._write_state(pending_state, str(repo))

    result = lc.complete(
        "abcd1234-issue-1",
        str(repo),
        "act-advance",
        1,
        {"pr_number": 123},
        lock.lease_token,
    )
    captured: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        loop_step.lc,
        "emit_loop_audit_event",
        lambda event_type, _project, payload: captured.append((event_type, payload)),
    )

    assert result.next_hint == "call propose again"
    loop_step._emit_loop_iteration(str(repo), pending_state, {"pr_number": 123})

    assert captured[0][0] == "loop_iteration"
    assert captured[0][1]["result"] == lc.Action.ADVANCE_PHASE.value


def test_loop_stop_counts_successful_checker_attempts(tmp_path: Path, monkeypatch: Any) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_running_state(repo)
    check_result = {
        "passed": True,
        "signature": "sig-1",
        "infrastructure_failure": False,
        "results": [],
    }
    lc.append_journal_event(
        "abcd1234-issue-1",
        str(repo),
        "completed",
        "checker",
        "act-check",
        {"action": lc.Action.RUN_CHECKER.value, "result": {"check_result": check_result}},
    )
    state = lc.load_state("abcd1234-issue-1", str(repo))
    state.status = "passed"
    lc._write_state(state, str(repo))
    captured: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        loop_step.lc,
        "emit_loop_audit_event",
        lambda event_type, _project, payload: captured.append((event_type, payload)),
    )

    loop_step._emit_loop_stop(str(repo), "abcd1234-issue-1", lc.Action.EXIT_SUCCESS.value, {})

    assert captured[0][0] == "loop_stop"
    assert captured[0][1]["iterations_total"] == 1


def test_propose_checker_and_advance_phase_params_follow_definition(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)
    _complete(repo, started["loop_id"], started, started["lease_token"])

    checker = _propose(repo, started["loop_id"], started["lease_token"])

    assert checker["action"] == lc.Action.RUN_CHECKER.value
    assert checker["params"]["mechanical"]["commands"] == ["pytest -q", "ruff check ."]
    assert checker["params"]["llm_review"]["baseline"] == "code-reviewer"
    assert checker["params"]["llm_review"]["selection"] == "skill-review-policy"

    mechanical = loop_step.lc.CheckResult(True, "mechanical", "", [], "mechanical.json")
    llm_review = loop_step.lc.CheckResult(
        True,
        "llm_review",
        loop_step.lc.compute_llm_review_signature([]),
        [],
        "review.json",
    )
    check_result = loop_step.lc.phase_check_to_dict(
        loop_step.lc.PhaseCheckResult(
            True,
            [mechanical, llm_review],
            "",
            False,
            metadata={"reviewers": ["code-reviewer"]},
        )
    )
    lc.save_artifact(
        started["loop_id"],
        str(repo),
        checker["action_id"],
        "check_result.json",
        json.dumps(check_result),
    )
    _complete(
        repo,
        started["loop_id"],
        checker,
        started["lease_token"],
        {"check_result": check_result},
    )

    advance = _propose(repo, started["loop_id"], started["lease_token"])

    assert advance["action"] == lc.Action.ADVANCE_PHASE.value
    assert advance["params"]["verified_branch"] == "loop/issue-7"
    assert advance["params"]["next_phase"] == "pr_review_response"
    assert advance["params"]["exec"] == ["commit", "push", "pr_create"]
    assert advance["params"]["issue_number"] == 7
    assert advance["params"]["repo_identity_verified"] is True


@pytest.mark.parametrize("action", list(lc.Action))
def test_proposal_response_adds_common_state_context_to_every_action(
    tmp_path: Path, action: lc.Action
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    loop_id = f"{loop_step.wm.resolve_repo_identity_hash(str(repo))}-issue-42"
    state = lc._initial_state(
        loop_id,
        "issue-loop",
        loop_step.wm.resolve_repo_identity_hash(str(repo)),
        str(repo),
        "main",
        "implementation",
    )
    lc._write_state(state, str(repo))
    proposal = loop_step.lc.ProposeResult(
        action.value,
        "act-context",
        1,
        "implementation",
        "implementation",
        1,
        {"params": {"action_specific": True}},
    )

    response = loop_step._proposal_response(loop_id, proposal, "test context", project=str(repo))

    assert response["params"]["action_specific"] is True
    assert response["params"]["issue_number"] == 42
    assert response["params"]["worktree_path"] == str(repo)
    assert response["params"]["branch"] == "main"
    assert response["params"]["repo_identity_verified"] is True


def test_common_proposal_params_uses_public_repo_identity_api(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = lc._initial_state(
        "abcd1234-issue-42",
        "issue-loop",
        loop_step.wm.resolve_repo_identity_hash(str(repo)),
        str(repo),
        "main",
        "implementation",
    )
    verified_states: list[Any] = []

    def verify(candidate: Any) -> bool:
        verified_states.append(candidate)
        return True

    monkeypatch.setattr(loop_step.lc, "is_repo_identity_verified", verify)

    params = loop_step._common_proposal_params("abcd1234-issue-42", state)

    assert params["repo_identity_verified"] is True
    assert verified_states == [state]


def test_run_maker_params_include_only_redacted_previous_check_summary(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    loop_id = f"{loop_step.wm.resolve_repo_identity_hash(str(repo))}-issue-7"
    state = lc._initial_state(
        loop_id,
        "issue-loop",
        loop_step.wm.resolve_repo_identity_hash(str(repo)),
        str(repo),
        "main",
        "implementation",
    )
    state.status = "running"
    state.last_check_result = {
        "passed": False,
        "signature": "phase-signature",
        "infrastructure_failure": False,
        "results": [
            {
                "passed": False,
                "layer": "mechanical",
                "signature": "mechanical-signature",
                "findings": [],
                "raw_artifact_path": "artifacts/act-old/mechanical.json",
                "infrastructure_failure": False,
            },
            {
                "passed": False,
                "layer": "llm_review",
                "signature": "review-signature",
                "findings": [
                    {
                        "severity": "high",
                        "summary": "token: do-not-expose",
                        "source": "code-reviewer",
                        "path": "app.py",
                        "line": 9,
                    },
                    {
                        "severity": "medium",
                        "summary": "optional detail",
                        "source": "code-reviewer",
                        "path": None,
                        "line": None,
                    },
                ],
                "raw_artifact_path": "review.json",
                "infrastructure_failure": False,
            },
        ],
    }
    lc._write_state(state, str(repo))
    lock = lc.acquire_lock(loop_id, str(repo), "owner", 3600, host=socket.gethostname())
    assert lock is not None

    response = _propose(repo, loop_id, lock.lease_token)

    previous = response["params"]["previous_check"]
    assert response["action"] == lc.Action.RUN_MAKER.value
    assert previous["mechanical"] == {
        "passed": False,
        "signature": "mechanical-signature",
        "infrastructure_failure": False,
        "raw_artifact_path": "artifacts/act-old/mechanical.json",
    }
    assert previous["critical_high"] == [
        {
            "severity": "high",
            "summary": "[REDACTED]",
            "source": "code-reviewer",
            "path": "app.py",
            "line": 9,
        }
    ]


def test_attach_restores_previous_check_for_retried_maker(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    loop_id = f"{loop_step.wm.resolve_repo_identity_hash(str(repo))}-issue-7"
    state = lc._initial_state(
        loop_id,
        "issue-loop",
        loop_step.wm.resolve_repo_identity_hash(str(repo)),
        str(repo),
        "main",
        "implementation",
    )
    state.status = "running"
    state.pending_action = lc.PendingAction(
        "act-check", lc.Action.RUN_CHECKER.value, "implementation", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, str(repo))
    mechanical = loop_step.lc.CheckResult(
        False, "mechanical", "failed-tests", [], "artifacts/act-check/mechanical.json"
    )
    review_finding = loop_step.lc.Finding("high", "Fix the race", "code-reviewer", "app.py", 12)
    llm_review = loop_step.lc.CheckResult(
        False,
        "llm_review",
        loop_step.lc.compute_llm_review_signature([review_finding]),
        [review_finding],
        "review.json",
    )
    phase_check = loop_step.lc.PhaseCheckResult(
        False,
        [mechanical, llm_review],
        "failed-tests",
        False,
        metadata={"reviewers": ["code-reviewer"]},
    )
    lc.save_artifact(
        loop_id,
        str(repo),
        "act-check",
        "check_result.json",
        json.dumps(loop_step.lc.phase_check_to_dict(phase_check)),
    )
    lock = lc.acquire_lock(loop_id, str(repo), "old-owner", 3600, host=socket.gethostname())
    assert lock is not None
    _set_lock_heartbeat(repo, loop_id, (datetime.now(UTC) - timedelta(seconds=7200)).isoformat())

    proc = _run_cli(["attach", "--loop-id", loop_id, "--project", str(repo)])
    response = _payload(proc)

    assert proc.returncode == 0, proc.stderr
    assert response["action"] == lc.Action.RUN_MAKER.value
    assert response["params"]["previous_check"]["mechanical"]["signature"] == "failed-tests"
    assert response["params"]["previous_check"]["critical_high"][0]["severity"] == "high"


def test_complete_checker_rejects_unsealed_caller_result(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)
    _complete(repo, started["loop_id"], started, started["lease_token"])
    checker = _propose(repo, started["loop_id"], started["lease_token"])
    check_result = {
        "passed": False,
        "signature": "sig-1",
        "infrastructure_failure": False,
        "results": [],
    }

    proc = _run_cli(
        [
            "complete",
            "--loop-id",
            started["loop_id"],
            "--action-id",
            checker["action_id"],
            "--state-version",
            str(checker["state_version"]),
            "--result",
            json.dumps({"check_result": check_result}),
            "--lease-token",
            started["lease_token"],
            "--project",
            str(repo),
        ]
    )

    artifact = lc.load_artifact(
        started["loop_id"], str(repo), checker["action_id"], "check_result.json"
    )
    assert proc.returncode == 2
    assert _payload(proc)["error"]["code"] == "protocol_violation"
    assert artifact is None


def test_complete_checker_does_not_overwrite_mismatched_sealed_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)
    _complete(repo, started["loop_id"], started, started["lease_token"])
    checker = _propose(repo, started["loop_id"], started["lease_token"])
    mechanical = loop_step.lc.CheckResult(True, "mechanical", "mech", [], "mechanical.json")
    llm_review = loop_step.lc.CheckResult(
        True,
        "llm_review",
        loop_step.lc.compute_llm_review_signature([]),
        [],
        "review.json",
    )
    check_result = loop_step.lc.phase_check_to_dict(
        loop_step.lc.PhaseCheckResult(
            True,
            [mechanical, llm_review],
            "",
            False,
            metadata={"reviewers": ["code-reviewer"]},
        )
    )
    lc.save_artifact(
        started["loop_id"],
        str(repo),
        checker["action_id"],
        "check_result.json",
        json.dumps(check_result),
    )
    mismatched = {**check_result, "signature": "caller-forgery"}

    proc = _run_cli(
        [
            "complete",
            "--loop-id",
            started["loop_id"],
            "--action-id",
            checker["action_id"],
            "--state-version",
            str(checker["state_version"]),
            "--result",
            json.dumps(mismatched),
            "--lease-token",
            started["lease_token"],
            "--project",
            str(repo),
        ]
    )

    artifact = lc.load_artifact(
        started["loop_id"], str(repo), checker["action_id"], "check_result.json"
    )
    assert proc.returncode == 2
    assert artifact is not None
    assert json.loads(artifact) == check_result


def test_complete_checker_rejects_matching_empty_sealed_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)
    _complete(repo, started["loop_id"], started, started["lease_token"])
    checker = _propose(repo, started["loop_id"], started["lease_token"])
    empty_result = {
        "passed": True,
        "signature": "",
        "infrastructure_failure": False,
        "results": [],
    }
    lc.save_artifact(
        started["loop_id"],
        str(repo),
        checker["action_id"],
        "check_result.json",
        json.dumps(empty_result),
    )

    proc = _run_cli(
        [
            "complete",
            "--loop-id",
            started["loop_id"],
            "--action-id",
            checker["action_id"],
            "--state-version",
            str(checker["state_version"]),
            "--result",
            json.dumps(empty_result),
            "--lease-token",
            started["lease_token"],
            "--project",
            str(repo),
        ]
    )

    assert proc.returncode == 2
    assert _payload(proc)["error"]["code"] == "protocol_violation"


def test_complete_checker_keeps_legacy_artifact_path_outside_implementation(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)
    _complete(repo, started["loop_id"], started, started["lease_token"])
    checker = _propose(repo, started["loop_id"], started["lease_token"])
    state = lc.load_state(started["loop_id"], str(repo))
    assert state.pending_action is not None
    state.phase = "pr_review_response"
    state.guards["pr_review_response"] = lc.GuardCounters()
    state.pending_action.phase = "pr_review_response"
    lc._write_state(state, str(repo))
    caller_result = {
        "passed": False,
        "signature": "legacy-result",
        "infrastructure_failure": False,
        "results": [],
    }

    _complete(
        repo,
        started["loop_id"],
        checker,
        started["lease_token"],
        caller_result,
    )

    artifact = lc.load_artifact(
        started["loop_id"], str(repo), checker["action_id"], "check_result.json"
    )
    assert artifact is not None
    assert json.loads(artifact) == caller_result


def test_run_checker_uses_state_and_definition_and_returns_complete_ready_json(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    commands = ["python -m custom_checker", "ruff format --check ."]
    _override_implementation_commands(repo, commands)
    started = _start(repo)
    _complete(repo, started["loop_id"], started, started["lease_token"])
    checker = _propose(repo, started["loop_id"], started["lease_token"])
    state = lc.load_state(started["loop_id"], str(repo))
    first_finding = loop_step.lc.Finding(
        "low", "First reviewer note", "code-reviewer", "first.py", 10
    )
    second_finding = loop_step.lc.Finding(
        "medium", "Second reviewer note", "security-reviewer", "second.py", 20
    )
    llm_paths = [
        _write_llm_result(tmp_path / "review-one.json", findings=[first_finding]),
        _write_llm_result(tmp_path / "review-two.json", findings=[second_finding]),
    ]
    captured: dict[str, Any] = {}

    def run_mechanical_checks(
        actual_commands: list[str],
        cwd: str,
        timeout_seconds: int,
        heartbeat: Any = None,
        artifact_writer: Any = None,
    ) -> list[Any]:
        captured["mechanical"] = (actual_commands, cwd, timeout_seconds)
        if heartbeat is not None:
            heartbeat()
        if artifact_writer is not None:
            artifact_writer(1, actual_commands[0], "mechanical output", 0)
        return []

    def combine_check_results(
        results: list[Any], pass_criteria: dict[str, int], required_layers: frozenset[str]
    ) -> Any:
        captured["combine"] = (results, pass_criteria, required_layers)
        combined = loop_step.lc.PhaseCheckResult(True, results, "", False)
        captured["combined"] = combined
        return combined

    monkeypatch.setattr(loop_step.lc, "run_mechanical_checks", run_mechanical_checks)
    monkeypatch.setattr(loop_step.lc, "combine_check_results", combine_check_results)

    exit_code = loop_step.main(_run_checker_args(repo, started, checker, llm_paths))

    output = capsys.readouterr().out
    payload = json.loads(output)
    results, pass_criteria, required_layers = captured["combine"]
    llm_result = next(item for item in results if item.layer == "llm_review")
    assert exit_code == 0
    assert output.count("\n") == 1
    assert captured["mechanical"][0] == commands
    assert captured["mechanical"][1] == state.worktree_path
    assert captured["mechanical"][2] > 0
    assert pass_criteria == {"critical": 0, "high": 0}
    assert required_layers == frozenset({"mechanical", "llm_review"})
    assert [finding.summary for finding in llm_result.findings] == [
        "First reviewer note",
        "Second reviewer note",
    ]
    assert llm_result.signature == loop_step.lc.compute_llm_review_signature(llm_result.findings)
    assert payload == {
        **loop_step.lc.phase_check_to_dict(captured["combined"]),
        "metadata": {"reviewers": ["code-reviewer", "security-reviewer"]},
    }
    assert "check_result" not in payload
    assert lc.load_state(started["loop_id"], str(repo)).pending_action is not None

    result_path = tmp_path / "phase-check.json"
    result_path.write_text(output, encoding="utf-8")
    completed = _run_cli(
        [
            "complete",
            "--loop-id",
            started["loop_id"],
            "--action-id",
            checker["action_id"],
            "--state-version",
            str(checker["state_version"]),
            "--result",
            f"@{result_path}",
            "--lease-token",
            started["lease_token"],
            "--project",
            str(repo),
        ]
    )
    assert completed.returncode == 0, completed.stderr


def test_run_checker_seals_redacted_finding_that_complete_accepts(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    repo = tmp_path / "repo"
    started, checker = _prepare_pending_checker(repo)
    secret = "super-secret-review-token"
    finding = loop_step.lc.Finding(
        "high",
        f"api_key: {secret}, rotate it",
        "code-reviewer",
        "settings.py",
        12,
    )
    llm_path = _write_llm_result(tmp_path / "review.json", findings=[finding])

    monkeypatch.setattr(
        loop_step.lc,
        "run_mechanical_checks",
        lambda *_args, **_kwargs: [],
    )

    exit_code = loop_step.main(_run_checker_args(repo, started, checker, [llm_path]))

    output = capsys.readouterr().out
    sealed = json.loads(output)
    llm_review = next(item for item in sealed["results"] if item["layer"] == "llm_review")
    assert exit_code == 0
    assert secret not in output
    assert llm_review["findings"][0]["summary"] == "[REDACTED], rotate it"
    reviewer_artifact = lc.load_artifact(
        started["loop_id"],
        str(repo),
        checker["action_id"],
        "llm_review_code-reviewer.json",
    )
    assert reviewer_artifact is not None
    reviewer_result = json.loads(reviewer_artifact)
    assert reviewer_result["passed"] is False
    assert reviewer_result["signature"] == llm_review["signature"]
    assert reviewer_result["findings"] == llm_review["findings"]

    result_path = tmp_path / "sealed-check-result.json"
    result_path.write_text(output, encoding="utf-8")
    completed = _run_cli(
        [
            "complete",
            "--loop-id",
            started["loop_id"],
            "--action-id",
            checker["action_id"],
            "--state-version",
            str(checker["state_version"]),
            "--result",
            f"@{result_path}",
            "--lease-token",
            started["lease_token"],
            "--project",
            str(repo),
        ]
    )

    assert completed.returncode == 0, completed.stdout


@pytest.mark.parametrize(
    ("invalid_part", "expected_code"),
    [
        ("lease", "lease_mismatch"),
        ("action_id", "stale_action"),
        ("state_version", "stale_action"),
        ("pending_action", "protocol_violation"),
    ],
)
def test_run_checker_rejects_invalid_protocol_before_mechanical_subprocess(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    invalid_part: str,
    expected_code: str,
) -> None:
    repo = tmp_path / "repo"
    started, checker = _prepare_pending_checker(repo)
    llm_path = _write_llm_result(tmp_path / "review.json")
    args = _run_checker_args(repo, started, checker, [llm_path])
    if invalid_part == "lease":
        args[args.index("--lease-token") + 1] = "wrong-token"
    elif invalid_part == "action_id":
        args[args.index("--action-id") + 1] = "act-stale"
    elif invalid_part == "state_version":
        args[args.index("--state-version") + 1] = str(checker["state_version"] + 1)
    else:
        state = lc.load_state(started["loop_id"], str(repo))
        state.pending_action = lc.PendingAction(
            checker["action_id"],
            lc.Action.RUN_MAKER.value,
            state.phase,
            checker["iteration"],
            lc.now_iso(),
        )
        lc._write_state(state, str(repo))

    def unexpected_mechanical_subprocess(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise AssertionError("mechanical subprocess must not run before protocol validation")

    monkeypatch.setattr(loop_step.lc, "run_mechanical_checks", unexpected_mechanical_subprocess)

    exit_code = loop_step.main(args)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"]["code"] == expected_code


def test_run_checker_revalidates_lease_before_writing_artifacts(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    repo = tmp_path / "repo"
    started, checker = _prepare_pending_checker(repo)
    llm_path = _write_llm_result(tmp_path / "review.json")

    def replace_lease_then_heartbeat(
        _commands: list[str],
        _cwd: str,
        _timeout_seconds: int,
        heartbeat: Any = None,
        artifact_writer: Any = None,
    ) -> list[Any]:
        del artifact_writer
        lock_path = lc.lock_path(started["loop_id"], str(repo))
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["lease_token"] = "replacement-token"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        assert heartbeat is not None
        heartbeat()
        return []

    monkeypatch.setattr(loop_step.lc, "run_mechanical_checks", replace_lease_then_heartbeat)

    exit_code = loop_step.main(_run_checker_args(repo, started, checker, [llm_path]))

    payload = json.loads(capsys.readouterr().out)
    artifact_dir = (
        repo / ".claude" / "loop" / started["loop_id"] / "artifacts" / checker["action_id"]
    )
    assert exit_code == 2
    assert payload["error"]["code"] == "lease_mismatch"
    assert not artifact_dir.exists()


def test_run_checker_persists_phase_and_mechanical_artifacts(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    repo = tmp_path / "repo"
    started, checker = _prepare_pending_checker(repo)
    llm_path = _write_llm_result(tmp_path / "review.json")
    failure = loop_step.lc.MechanicalFailure(
        "pytest -q", "test_failure", "assertion", "FAILED tests/test_example.py::test_case"
    )
    monkeypatch.setattr(loop_step.lc, "run_mechanical_checks", lambda *_args, **_kwargs: [failure])

    exit_code = loop_step.main(_run_checker_args(repo, started, checker, [llm_path]))

    payload = json.loads(capsys.readouterr().out)
    phase_artifact = lc.load_artifact(
        started["loop_id"], str(repo), checker["action_id"], "check_result.json"
    )
    mechanical_artifact = lc.load_artifact(
        started["loop_id"], str(repo), checker["action_id"], "mechanical.json"
    )
    assert exit_code == 0
    assert phase_artifact is not None
    assert json.loads(phase_artifact) == payload
    assert mechanical_artifact is not None
    mechanical_payload = json.loads(mechanical_artifact)
    assert "pytest -q" in json.dumps(mechanical_payload)
    assert "test_failure" in json.dumps(mechanical_payload)
    assert "FAILED tests/test_example.py::test_case" in json.dumps(mechanical_payload)


def test_run_checker_persists_self_contained_layer_artifacts(tmp_path: Path, capsys: Any) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _override_implementation_commands(repo, ["printf mechanical-raw-output"])
    started = _start(repo)
    _complete(repo, started["loop_id"], started, started["lease_token"])
    checker = _propose(repo, started["loop_id"], started["lease_token"])
    llm_path = _write_llm_result(tmp_path / "review.json")

    exit_code = loop_step.main(_run_checker_args(repo, started, checker, [llm_path]))

    payload = json.loads(capsys.readouterr().out)
    mechanical = next(item for item in payload["results"] if item["layer"] == "mechanical")
    llm_review = next(item for item in payload["results"] if item["layer"] == "llm_review")
    mechanical_artifact = lc.artifact_path(
        started["loop_id"], str(repo), checker["action_id"], "mechanical_1.log"
    )
    reviewer_artifact = lc.artifact_path(
        started["loop_id"],
        str(repo),
        checker["action_id"],
        "llm_review_code-reviewer.json",
    )

    assert exit_code == 0
    assert mechanical["raw_artifact_path"] == (f"artifacts/{checker['action_id']}/mechanical_1.log")
    assert llm_review["raw_artifact_path"] == (
        f"artifacts/{checker['action_id']}/llm_review_code-reviewer.json"
    )
    assert mechanical_artifact.read_text(encoding="utf-8") == "mechanical-raw-output"
    assert json.loads(reviewer_artifact.read_text(encoding="utf-8"))["layer"] == "llm_review"
    assert mechanical_artifact.stat().st_mode & 0o777 == 0o600
    assert reviewer_artifact.stat().st_mode & 0o777 == 0o600


def test_run_checker_propagates_mechanical_infrastructure_failure(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    repo = tmp_path / "repo"
    started, checker = _prepare_pending_checker(repo)
    llm_path = _write_llm_result(tmp_path / "review.json")
    failure = loop_step.lc.MechanicalFailure(
        "pytest -q", "infrastructure_failure", "rate_limit", "service unavailable"
    )
    monkeypatch.setattr(loop_step.lc, "run_mechanical_checks", lambda *_args, **_kwargs: [failure])

    exit_code = loop_step.main(_run_checker_args(repo, started, checker, [llm_path]))

    payload = json.loads(capsys.readouterr().out)
    mechanical = next(item for item in payload["results"] if item["layer"] == "mechanical")
    assert exit_code == 0
    assert mechanical["passed"] is False
    assert mechanical["infrastructure_failure"] is True
    assert payload["passed"] is False
    assert payload["infrastructure_failure"] is True


@pytest.mark.parametrize(
    ("severity", "reported_passed", "expected_passed"),
    [("high", True, False), (None, False, True)],
)
def test_run_checker_normalizes_llm_passed_from_findings(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    severity: str | None,
    reported_passed: bool,
    expected_passed: bool,
) -> None:
    repo = tmp_path / "repo"
    started, checker = _prepare_pending_checker(repo)
    findings = (
        [loop_step.lc.Finding(severity, "Review finding", "code-reviewer")]
        if severity is not None
        else []
    )
    llm_path = _write_llm_result(
        tmp_path / "review.json", findings=findings, passed=reported_passed
    )
    monkeypatch.setattr(loop_step.lc, "run_mechanical_checks", lambda *_args, **_kwargs: [])

    exit_code = loop_step.main(_run_checker_args(repo, started, checker, [llm_path]))

    payload = json.loads(capsys.readouterr().out)
    llm_review = next(item for item in payload["results"] if item["layer"] == "llm_review")
    assert exit_code == 0
    assert llm_review["passed"] is expected_passed
    assert payload["passed"] is expected_passed


@pytest.mark.parametrize("unavailable_kind", ["missing", "invalid_json", "wrong_layer"])
def test_run_checker_rejects_invalid_llm_result_file(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    unavailable_kind: str,
) -> None:
    repo = tmp_path / "repo"
    started, checker = _prepare_pending_checker(repo)
    llm_path = tmp_path / "unavailable-review.json"
    if unavailable_kind == "invalid_json":
        llm_path.write_text("{not-json", encoding="utf-8")
        llm_path.chmod(0o600)
    elif unavailable_kind == "wrong_layer":
        _write_llm_result(llm_path, layer="mechanical")
    mechanical_calls = 0

    def run_mechanical_checks(*_args: Any) -> list[Any]:
        nonlocal mechanical_calls
        mechanical_calls += 1
        return []

    monkeypatch.setattr(loop_step.lc, "run_mechanical_checks", run_mechanical_checks)

    exit_code = loop_step.main(_run_checker_args(repo, started, checker, [llm_path]))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert mechanical_calls == 0
    assert payload["error"]["code"] == "invalid_llm_result"


@pytest.mark.parametrize(
    "bindings",
    [
        ["security-reviewer=@review.json"],
        ["code-reviewer=@review.json", "code-reviewer=@review.json"],
        [
            "code-reviewer=@review.json",
            "security-reviewer=@review.json",
            "performance-reviewer=@review.json",
        ],
    ],
)
def test_run_checker_rejects_invalid_reviewer_configuration(
    tmp_path: Path, monkeypatch: Any, capsys: Any, bindings: list[str]
) -> None:
    repo = tmp_path / "repo"
    started, checker = _prepare_pending_checker(repo)
    review_path = _write_llm_result(tmp_path / "review.json")
    args = _run_checker_args(repo, started, checker, [])
    for binding in bindings:
        args.extend(["--llm-result", binding.replace("@review.json", f"@{review_path}")])
    monkeypatch.setattr(
        loop_step.lc,
        "run_mechanical_checks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid reviewer config must fail before commands")
        ),
    )

    exit_code = loop_step.main(args)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"]["code"] == "invalid_llm_result"


def test_run_checker_normalizes_finding_source_to_bound_reviewer(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    repo = tmp_path / "repo"
    started, checker = _prepare_pending_checker(repo)
    finding = loop_step.lc.Finding("high", "Must fix", "forged-reviewer", "app.py", 7)
    llm_path = _write_llm_result(tmp_path / "review.json", findings=[finding])
    monkeypatch.setattr(loop_step.lc, "run_mechanical_checks", lambda *_args, **_kwargs: [])

    exit_code = loop_step.main(_run_checker_args(repo, started, checker, [llm_path]))

    payload = json.loads(capsys.readouterr().out)
    llm_review = next(item for item in payload["results"] if item["layer"] == "llm_review")
    assert exit_code == 0
    assert llm_review["findings"][0]["source"] == "code-reviewer"


def test_run_checker_accepts_canonical_reviewer_infrastructure_result(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    repo = tmp_path / "repo"
    started, checker = _prepare_pending_checker(repo)
    llm_path = _write_llm_result(
        tmp_path / "review.json", infrastructure_failure=True, passed=False
    )
    monkeypatch.setattr(loop_step.lc, "run_mechanical_checks", lambda *_args, **_kwargs: [])

    exit_code = loop_step.main(_run_checker_args(repo, started, checker, [llm_path]))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["infrastructure_failure"] is True


@pytest.mark.parametrize("boundary", ["symlink", "mode", "size"])
def test_run_checker_rejects_unsafe_reviewer_result_file(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    boundary: str,
) -> None:
    repo = tmp_path / "repo"
    started, checker = _prepare_pending_checker(repo)
    source = _write_llm_result(tmp_path / "source.json")
    llm_path = source
    if boundary == "symlink":
        llm_path = tmp_path / "review-link.json"
        llm_path.symlink_to(source)
    elif boundary == "mode":
        source.chmod(0o644)
    else:
        source.write_bytes(b" " * (loop_step.MAX_LLM_RESULT_BYTES + 1))
        source.chmod(0o600)
    monkeypatch.setattr(
        loop_step.lc,
        "run_mechanical_checks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe result file must fail before commands")
        ),
    )

    exit_code = loop_step.main(_run_checker_args(repo, started, checker, [llm_path]))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"]["code"] == "invalid_llm_result"


def test_start_existing_state_returns_already_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    first = _start(repo)

    proc = _run_cli(["start", "--issue", "7", "--project", str(repo)])
    payload = _payload(proc)

    assert proc.returncode == 1
    assert payload["error"]["code"] == "already_exists"
    assert first["loop_id"] in payload["error"]["message"]
    assert "already_exists" in proc.stderr


def test_direct_main_missing_lease_token_is_validation_rejection(capsys: Any) -> None:
    exit_code = loop_step.main(
        ["propose", "--loop-id", "abcd1234-issue-1", "--project", str(REPO_ROOT)]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 2
    assert payload["error"]["code"] == "lease_token_required"
    assert "lease_token_required" in captured.err


def test_complete_missing_lease_token_is_validation_rejection(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)

    proc = _run_cli(
        [
            "complete",
            "--loop-id",
            started["loop_id"],
            "--action-id",
            started["action_id"],
            "--state-version",
            str(started["state_version"]),
            "--result",
            "{}",
            "--project",
            str(repo),
        ]
    )
    payload = _payload(proc)

    assert proc.returncode == 2
    assert payload["error"]["code"] == "lease_token_required"


def test_propose_wrong_live_same_host_lease_returns_exit_2(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)

    proc = _run_cli(
        [
            "propose",
            "--loop-id",
            started["loop_id"],
            "--lease-token",
            "wrong-token",
            "--project",
            str(repo),
        ]
    )
    payload = _payload(proc)

    assert proc.returncode == 2
    assert payload["error"]["code"] == "lease_mismatch"


def test_complete_wrong_live_same_host_lease_returns_exit_2(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)

    proc = _run_cli(
        [
            "complete",
            "--loop-id",
            started["loop_id"],
            "--action-id",
            started["action_id"],
            "--state-version",
            str(started["state_version"]),
            "--result",
            "{}",
            "--lease-token",
            "wrong-token",
            "--project",
            str(repo),
        ]
    )
    payload = _payload(proc)

    assert proc.returncode == 2
    assert payload["error"]["code"] == "lease_mismatch"


def test_propose_refreshes_active_lease(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)
    _complete(repo, started["loop_id"], started, started["lease_token"])
    old_heartbeat = _old_live_heartbeat()
    _set_lock_heartbeat(repo, started["loop_id"], old_heartbeat)

    _propose(repo, started["loop_id"], started["lease_token"])

    lock = json.loads(lc.lock_path(started["loop_id"], str(repo)).read_text(encoding="utf-8"))
    assert lock["heartbeat_at"] != old_heartbeat


def test_complete_refreshes_active_lease(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)
    old_heartbeat = _old_live_heartbeat()
    _set_lock_heartbeat(repo, started["loop_id"], old_heartbeat)

    _complete(repo, started["loop_id"], started, started["lease_token"])

    lock = json.loads(lc.lock_path(started["loop_id"], str(repo)).read_text(encoding="utf-8"))
    assert lock["heartbeat_at"] != old_heartbeat


def test_reconcile_refreshes_active_lease(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)
    old_heartbeat = _old_live_heartbeat()
    _set_lock_heartbeat(repo, started["loop_id"], old_heartbeat)

    proc = _run_cli(
        [
            "reconcile",
            "--loop-id",
            started["loop_id"],
            "--lease-token",
            started["lease_token"],
            "--project",
            str(repo),
        ]
    )

    assert proc.returncode == 0, proc.stderr
    lock = json.loads(lc.lock_path(started["loop_id"], str(repo)).read_text(encoding="utf-8"))
    assert lock["heartbeat_at"] != old_heartbeat


def test_complete_stale_action_returns_exit_2(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)

    proc = _run_cli(
        [
            "complete",
            "--loop-id",
            started["loop_id"],
            "--action-id",
            "act-stale",
            "--state-version",
            str(started["state_version"]),
            "--result",
            "{}",
            "--lease-token",
            started["lease_token"],
            "--project",
            str(repo),
        ]
    )
    payload = _payload(proc)

    assert proc.returncode == 2
    assert payload["error"]["code"] == "stale_action"


def test_wrong_lease_token_returns_exit_2(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)

    proc = _run_cli(
        [
            "heartbeat",
            "--loop-id",
            started["loop_id"],
            "--lease-token",
            "wrong-token",
            "--project",
            str(repo),
        ]
    )
    payload = _payload(proc)

    assert proc.returncode == 2
    assert payload["error"]["code"] == "lease_mismatch"


def test_reconcile_wrong_lease_token_returns_exit_2(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)

    proc = _run_cli(
        [
            "reconcile",
            "--loop-id",
            started["loop_id"],
            "--lease-token",
            "wrong-token",
            "--project",
            str(repo),
        ]
    )
    payload = _payload(proc)

    assert proc.returncode == 2
    assert payload["error"]["code"] == "lease_mismatch"


def test_heartbeat_does_not_touch_state_version(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)
    before = lc.load_state(started["loop_id"], str(repo)).state_version

    proc = _run_cli(
        [
            "heartbeat",
            "--loop-id",
            started["loop_id"],
            "--lease-token",
            started["lease_token"],
            "--project",
            str(repo),
        ]
    )
    payload = _payload(proc)
    after = lc.load_state(started["loop_id"], str(repo)).state_version

    assert proc.returncode == 0
    assert payload["loop_id"] == started["loop_id"]
    assert payload["ttl"] == 3600
    assert after == before


def test_attach_rejects_live_lease_with_exit_3(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)
    complete = _run_cli(
        [
            "complete",
            "--loop-id",
            started["loop_id"],
            "--action-id",
            started["action_id"],
            "--state-version",
            str(started["state_version"]),
            "--result",
            "{}",
            "--lease-token",
            started["lease_token"],
            "--project",
            str(repo),
        ]
    )
    assert complete.returncode == 0, complete.stderr

    proc = _run_cli(["attach", "--loop-id", started["loop_id"], "--project", str(repo)])
    payload = _payload(proc)

    assert proc.returncode == 3
    assert payload["error"]["code"] == "lock_unavailable"


def test_attach_propose_failure_returns_reclaimed_lease_token(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    stale_heartbeat = (datetime.now(UTC) - timedelta(seconds=7200)).isoformat()
    _set_lock_heartbeat(repo, "abcd1234-issue-1", stale_heartbeat)

    def fail_propose(
        _loop_id: str,
        _project: str,
        _lease_token: str,
        recover_orphans: bool = False,
    ) -> Any:
        raise loop_step.ld.DefinitionValidationError("definition drift")

    monkeypatch.setattr(loop_step.lc, "propose", fail_propose)

    exit_code = loop_step.main(["attach", "--loop-id", "abcd1234-issue-1", "--project", str(repo)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["error"]["code"] == "definition_invalid"
    assert payload["lease_token"] != lock.lease_token


def test_attach_propose_validation_failure_preserves_exit_2(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    stale_heartbeat = (datetime.now(UTC) - timedelta(seconds=7200)).isoformat()
    _set_lock_heartbeat(repo, "abcd1234-issue-1", stale_heartbeat)

    def fail_propose(
        _loop_id: str,
        _project: str,
        _lease_token: str,
        recover_orphans: bool = False,
    ) -> Any:
        raise loop_step.lc.ProtocolViolationError("pending action must be completed")

    monkeypatch.setattr(loop_step.lc, "propose", fail_propose)

    exit_code = loop_step.main(["attach", "--loop-id", "abcd1234-issue-1", "--project", str(repo)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 2
    assert payload["error"]["code"] == "protocol_violation"
    assert payload["lease_token"] != lock.lease_token


def test_attach_returns_rerunnable_pending_checker_after_reclaim(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    state = lc.load_state("abcd1234-issue-1", str(repo))
    state.pending_action = lc.PendingAction(
        "act-check", lc.Action.RUN_CHECKER.value, "implementation", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, str(repo))
    stale_heartbeat = (datetime.now(UTC) - timedelta(seconds=7200)).isoformat()
    _set_lock_heartbeat(repo, "abcd1234-issue-1", stale_heartbeat)

    proc = _run_cli(["attach", "--loop-id", "abcd1234-issue-1", "--project", str(repo)])
    payload = _payload(proc)

    assert proc.returncode == 0, proc.stderr
    assert payload["action"] == lc.Action.RUN_CHECKER.value
    assert payload["action_id"] == "act-check"
    assert payload["lease_token"] != lock.lease_token


def test_resume_requires_reset_counters(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)

    proc = _run_cli(["resume", "--loop-id", started["loop_id"], "--project", str(repo)])
    payload = _payload(proc)

    assert proc.returncode == 1
    assert payload["error"]["code"] == "reset_counters_required"


def test_resume_with_reset_returns_new_lease_and_proposal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    state = lc.load_state("abcd1234-issue-1", str(repo))
    state.status = "failed"
    lc._write_state(state, str(repo))

    proc = _run_cli(
        [
            "resume",
            "--loop-id",
            "abcd1234-issue-1",
            "--reset-counters",
            "--project",
            str(repo),
        ]
    )
    payload = _payload(proc)

    assert proc.returncode == 0, proc.stderr
    assert payload["action"] == lc.Action.RUN_MAKER.value
    assert payload["lease_token"] != lock.lease_token


def test_resume_propose_failure_returns_new_lease_token(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    state = lc.load_state("abcd1234-issue-1", str(repo))
    state.status = "failed"
    lc._write_state(state, str(repo))

    def fail_propose(_loop_id: str, _project: str, _lease_token: str) -> Any:
        raise loop_step.ld.DefinitionValidationError("definition drift")

    monkeypatch.setattr(loop_step.lc, "propose", fail_propose)

    exit_code = loop_step.main(
        ["resume", "--loop-id", "abcd1234-issue-1", "--reset-counters", "--project", str(repo)]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["error"]["code"] == "definition_invalid"
    assert payload["lease_token"] != lock.lease_token


def test_resume_propose_validation_failure_preserves_exit_2(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    state = lc.load_state("abcd1234-issue-1", str(repo))
    state.status = "failed"
    lc._write_state(state, str(repo))

    def fail_propose(_loop_id: str, _project: str, _lease_token: str) -> Any:
        raise loop_step.lc.ProtocolViolationError("pending action must be completed")

    monkeypatch.setattr(loop_step.lc, "propose", fail_propose)

    exit_code = loop_step.main(
        ["resume", "--loop-id", "abcd1234-issue-1", "--reset-counters", "--project", str(repo)]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 2
    assert payload["error"]["code"] == "protocol_violation"
    assert payload["lease_token"] != lock.lease_token


def test_reconcile_reports_marked_infrastructure_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)

    proc = _run_cli(
        [
            "reconcile",
            "--loop-id",
            started["loop_id"],
            "--lease-token",
            started["lease_token"],
            "--project",
            str(repo),
        ]
    )
    payload = _payload(proc)

    assert proc.returncode == 0, proc.stderr
    assert payload["reconciled"] is True
    assert payload["resolved_action_id"] == started["action_id"]
    assert payload["resolution"] == "marked_infrastructure_failure"


def test_reconcile_checker_rerun_required_maps_to_none_resolution(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    state = lc.load_state("abcd1234-issue-1", str(repo))
    state.pending_action = lc.PendingAction(
        "act-check", lc.Action.RUN_CHECKER.value, "implementation", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, str(repo))

    proc = _run_cli(
        [
            "reconcile",
            "--loop-id",
            "abcd1234-issue-1",
            "--lease-token",
            lock.lease_token,
            "--project",
            str(repo),
        ]
    )
    payload = _payload(proc)

    assert proc.returncode == 0, proc.stderr
    assert payload["reconciled"] is False
    assert payload["resolution"] == "none"


def test_propose_fails_when_phase_definition_loading_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    loop_dir = repo / ".claude" / "config" / "loop-harness" / "loops"
    loop_dir.mkdir(parents=True)
    (loop_dir / "broken.yaml").write_text("id: broken\n", encoding="utf-8")

    proc = _run_cli(
        [
            "propose",
            "--loop-id",
            "abcd1234-issue-1",
            "--lease-token",
            lock.lease_token,
            "--project",
            str(repo),
        ]
    )
    payload = _payload(proc)

    assert proc.returncode == 1
    assert payload["error"]["code"] == "definition_invalid"
    state = lc.load_state("abcd1234-issue-1", str(repo))
    assert state.pending_action is None
    assert state.state_version == 0


def test_exit_failure_params_include_stop_reason_and_draft_exec(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    state = lc.load_state("abcd1234-issue-1", str(repo))
    state.status = "failed"
    state.stop_reason = "max_iterations"
    state.pr_number = 123
    lc._write_state(state, str(repo))

    payload = _propose(repo, "abcd1234-issue-1", lock.lease_token)

    assert payload["action"] == lc.Action.EXIT_FAILURE.value
    assert payload["params"]["stop_reason"] == "max_iterations"
    assert payload["params"]["draft_pr_exec"] == ["pr_create_draft", "notify"]
    assert payload["params"]["pr_number"] == 123
    assert payload["params"]["issue_number"] == 1
    assert payload["params"]["repo_identity_verified"] is True


def test_complete_terminal_action_returns_terminal_next_hint(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    state = lc.load_state("abcd1234-issue-1", str(repo))
    state.status = "passed"
    lc._write_state(state, str(repo))
    proposal = _propose(repo, "abcd1234-issue-1", lock.lease_token)

    payload = _complete(repo, "abcd1234-issue-1", proposal, lock.lease_token)

    assert proposal["action"] == lc.Action.EXIT_SUCCESS.value
    assert payload["next"] == "loop terminal"


def test_external_signal_phase_proposes_wait_action(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    state = lc.load_state("abcd1234-issue-1", str(repo))
    state.phase = "pr_review_response"
    state.pr_number = 123
    state.guards["pr_review_response"] = lc.GuardCounters()
    state.last_completed_action = lc.LastCompletedAction(
        action_id="act-maker",
        state_version_before=1,
        state_version_after=2,
        result_digest="digest",
        completed_at=lc.now_iso(),
    )
    state.state_version = 2
    lc._write_state(state, str(repo))
    lc.append_journal_event(
        "abcd1234-issue-1",
        str(repo),
        "completed",
        "maker",
        "act-maker",
        {"action": lc.Action.RUN_MAKER.value, "result": {}},
    )

    payload = _propose(repo, "abcd1234-issue-1", lock.lease_token)

    assert payload["action"] == lc.Action.WAIT_EXTERNAL_REVIEW.value
    assert payload["params"]["pr_number"] == 123
    assert payload["params"]["push_required"] is True
    assert payload["params"]["verified_branch"] == "main"


@pytest.mark.parametrize(
    ("push_guard", "expected_reason"),
    [
        (
            {"branch_ok": False, "repo_identity_ok": True, "reason": "default_branch"},
            "push_guard_violation",
        ),
        (
            {
                "branch_ok": True,
                "repo_identity_ok": False,
                "reason": "repo_identity_mismatch",
            },
            "repo_identity_mismatch",
        ),
    ],
)
def test_push_required_wait_complete_stops_on_push_guard_failure(
    tmp_path: Path,
    push_guard: dict[str, Any],
    expected_reason: str,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    loop_id = "abcd1234-issue-1"
    state = lc.load_state(loop_id, str(repo))
    state.phase = "pr_review_response"
    state.pr_number = 123
    state.guards["pr_review_response"] = lc.GuardCounters()
    state.last_completed_action = lc.LastCompletedAction("act-maker", 1, 2, "digest", lc.now_iso())
    state.state_version = 2
    lc._write_state(state, str(repo))
    lc.append_journal_event(
        loop_id,
        str(repo),
        "completed",
        "maker",
        "act-maker",
        {"action": lc.Action.RUN_MAKER.value, "result": {}},
    )
    wait = _propose(repo, loop_id, lock.lease_token)
    assert wait["action"] == lc.Action.WAIT_EXTERNAL_REVIEW.value
    assert wait["params"]["push_required"] is True

    _complete(
        repo,
        loop_id,
        wait,
        lock.lease_token,
        {"push_guard": push_guard},
    )
    stopped = _propose(repo, loop_id, lock.lease_token)

    assert stopped["action"] == lc.Action.STOP.value
    assert stopped["params"]["stop_reason"] == expected_reason
    assert lc.load_state(loop_id, str(repo)).status == "stopped"


def test_external_signal_phase_waits_before_response_maker_after_advance(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    state = lc.load_state("abcd1234-issue-1", str(repo))
    state.phase = "pr_review_response"
    state.pr_number = 123
    state.guards["pr_review_response"] = lc.GuardCounters()
    state.last_completed_action = lc.LastCompletedAction(
        action_id="act-advance",
        state_version_before=1,
        state_version_after=2,
        result_digest="digest",
        completed_at=lc.now_iso(),
    )
    state.state_version = 2
    lc._write_state(state, str(repo))
    lc.append_journal_event(
        "abcd1234-issue-1",
        str(repo),
        "completed",
        "loop_step",
        "act-advance",
        {"action": lc.Action.ADVANCE_PHASE.value, "result": {"pr_number": 123}},
    )

    payload = _propose(repo, "abcd1234-issue-1", lock.lease_token)

    assert payload["action"] == lc.Action.WAIT_EXTERNAL_REVIEW.value
    assert payload["params"]["pr_number"] == 123
    assert payload["params"]["push_required"] is False
    assert payload["params"]["verified_branch"] == "main"


def test_external_wait_proposal_stops_when_branch_mismatches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    loop_id = f"{loop_step.wm.resolve_repo_identity_hash(str(repo))}-issue-7"
    state = lc._initial_state(
        loop_id,
        "issue-loop",
        loop_step.wm.resolve_repo_identity_hash(str(repo)),
        str(repo),
        "loop/issue-7",
        "pr_review_response",
    )
    state.status = "running"
    state.pr_number = 123
    state.last_completed_action = lc.LastCompletedAction(
        "act-advance", 1, 2, "digest", lc.now_iso()
    )
    state.state_version = 2
    lc._write_state(state, str(repo))
    lc.append_journal_event(
        loop_id,
        str(repo),
        "completed",
        "step",
        "act-advance",
        {"action": lc.Action.ADVANCE_PHASE.value, "result": {"pr_number": 123}},
    )
    lock = lc.acquire_lock(loop_id, str(repo), "owner", 3600, host=socket.gethostname())
    assert lock is not None

    response = _propose(repo, loop_id, lock.lease_token)

    assert response["action"] == lc.Action.STOP.value
    assert response["params"]["stop_reason"] == "push_guard_violation"


def test_advance_proposal_stops_when_branch_mismatches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    loop_id = "abcd1234-issue-1"
    repo_hash = loop_step.wm.resolve_repo_identity_hash(str(repo))
    state = lc._initial_state(
        loop_id,
        "issue-loop",
        repo_hash,
        str(repo),
        "loop/issue-1",
        "implementation",
    )
    state.status = "running"
    state.last_check_result = {"next_phase": "pr_review_response"}
    lc._write_state(state, str(repo))
    lock = lc.acquire_lock(loop_id, str(repo), "owner", 3600, host=socket.gethostname())
    assert lock is not None

    payload = _propose(repo, loop_id, lock.lease_token)

    assert payload["action"] == lc.Action.STOP.value
    assert payload["params"]["stop_reason"] == "push_guard_violation"
    assert lc.load_state(loop_id, str(repo)).status == "stopped"


def test_advance_proposal_stops_when_repo_identity_mismatches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    loop_id = "abcd1234-issue-1"
    state = lc._initial_state(
        loop_id,
        "issue-loop",
        "wronghash",
        str(repo),
        "main",
        "implementation",
    )
    state.status = "running"
    state.last_check_result = {"next_phase": "pr_review_response"}
    lc._write_state(state, str(repo))
    lock = lc.acquire_lock(loop_id, str(repo), "owner", 3600, host=socket.gethostname())
    assert lock is not None

    payload = _propose(repo, loop_id, lock.lease_token)

    assert payload["action"] == lc.Action.STOP.value
    assert payload["params"]["stop_reason"] == "repo_identity_mismatch"
    assert payload["params"]["repo_identity_verified"] is False
    assert lc.load_state(loop_id, str(repo)).status == "stopped"


def test_pending_checker_stops_before_replay_when_repo_identity_mismatches(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    loop_id = "abcd1234-issue-1"
    state = lc._initial_state(
        loop_id,
        "issue-loop",
        "wronghash",
        str(repo),
        "main",
        "implementation",
    )
    state.status = "running"
    state.pending_action = lc.PendingAction(
        "act-check", lc.Action.RUN_CHECKER.value, "implementation", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, str(repo))
    lock = lc.acquire_lock(loop_id, str(repo), "owner", 3600, host=socket.gethostname())
    assert lock is not None

    response = _propose(repo, loop_id, lock.lease_token)

    stopped = lc.load_state(loop_id, str(repo))
    assert response["action"] == lc.Action.STOP.value
    assert response["params"]["stop_reason"] == "repo_identity_mismatch"
    assert response["params"]["repo_identity_verified"] is False
    assert stopped.status == "stopped"
    stopped_event = lc.find_journal_event(loop_id, str(repo), None, "stopped")
    assert stopped_event is not None
    assert stopped_event["payload"]["replaced_action_id"] == "act-check"
    assert stopped_event["payload"]["replaced_action"] == lc.Action.RUN_CHECKER.value


def test_branch_mismatch_does_not_stop_non_advance_action(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    loop_id = f"{loop_step.wm.resolve_repo_identity_hash(str(repo))}-issue-7"
    state = lc._initial_state(
        loop_id,
        "issue-loop",
        loop_step.wm.resolve_repo_identity_hash(str(repo)),
        str(repo),
        "loop/issue-7",
        "implementation",
    )
    state.status = "running"
    lc._write_state(state, str(repo))
    lock = lc.acquire_lock(loop_id, str(repo), "owner", 3600, host=socket.gethostname())
    assert lock is not None

    response = _propose(repo, loop_id, lock.lease_token)

    assert response["action"] == lc.Action.RUN_MAKER.value
    assert response["params"]["repo_identity_verified"] is True


def test_foreign_live_lease_requires_valid_token_before_stop(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    loop_id = "abcd1234-issue-1"
    state = lc._initial_state(
        loop_id,
        "issue-loop",
        "abcd1234",
        str(repo / ".worktrees" / "loop-issue-1"),
        "loop/issue-1",
        "implementation",
    )
    state.status = "running"
    lc._write_state(state, str(repo))
    lock = lc.acquire_lock(loop_id, str(repo), "owner", 3600, host="definitely-foreign-host")
    assert lock is not None

    proc = _run_cli(
        [
            "propose",
            "--loop-id",
            loop_id,
            "--lease-token",
            "wrong-token",
            "--project",
            str(repo),
        ]
    )
    payload = _payload(proc)

    assert proc.returncode == 2
    assert payload["error"]["code"] == "lease_mismatch"
    assert lc.load_state(loop_id, str(repo)).status == "running"


def test_foreign_live_lease_stop_preserves_verified_repo_identity(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    loop_id = "abcd1234-issue-1"
    state = lc._initial_state(
        loop_id,
        "issue-loop",
        loop_step.wm.resolve_repo_identity_hash(str(repo)),
        str(repo),
        "main",
        "implementation",
    )
    state.status = "running"
    lc._write_state(state, str(repo))
    lock = lc.acquire_lock(loop_id, str(repo), "owner", 3600, host="foreign-host")
    assert lock is not None

    proc = _run_cli(
        [
            "propose",
            "--loop-id",
            loop_id,
            "--lease-token",
            lock.lease_token,
            "--project",
            str(repo),
        ]
    )
    response = _payload(proc)

    assert proc.returncode == 0, proc.stderr
    assert response["action"] == lc.Action.STOP.value
    assert response["params"]["repo_identity_verified"] is True


def test_foreign_live_lease_stop_is_pending_replayable_and_completable(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    loop_id = f"{loop_step.wm.resolve_repo_identity_hash(str(repo))}-issue-7"
    state = lc._initial_state(
        loop_id,
        "issue-loop",
        loop_step.wm.resolve_repo_identity_hash(str(repo)),
        str(repo),
        "main",
        "implementation",
    )
    state.status = "running"
    state.pending_action = lc.PendingAction(
        "act-old-maker", lc.Action.RUN_MAKER.value, "implementation", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, str(repo))
    lock = lc.acquire_lock(loop_id, str(repo), "owner", 3600, host="foreign-host")
    assert lock is not None

    first = _run_cli(
        [
            "propose",
            "--loop-id",
            loop_id,
            "--lease-token",
            lock.lease_token,
            "--project",
            str(repo),
        ]
    )
    first_payload = _payload(first)
    second = _run_cli(
        [
            "propose",
            "--loop-id",
            loop_id,
            "--lease-token",
            lock.lease_token,
            "--project",
            str(repo),
        ]
    )
    second_payload = _payload(second)
    pending = lc.load_state(loop_id, str(repo)).pending_action

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first_payload["action"] == lc.Action.STOP.value
    assert first_payload["action_id"] == second_payload["action_id"]
    assert first_payload["state_version"] == second_payload["state_version"]
    assert pending is not None
    assert pending.action == lc.Action.STOP.value
    assert pending.action_id == first_payload["action_id"]

    _complete(repo, loop_id, first_payload, lock.lease_token)
    assert lc.load_state(loop_id, str(repo)).pending_action is None
    old_complete = _run_cli(
        [
            "complete",
            "--loop-id",
            loop_id,
            "--action-id",
            "act-old-maker",
            "--state-version",
            "1",
            "--result",
            "{}",
            "--lease-token",
            lock.lease_token,
            "--project",
            str(repo),
        ]
    )
    assert old_complete.returncode == 2


def test_foreign_live_lease_stop_reports_identity_mismatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    loop_id = "abcd1234-issue-1"
    state = lc._initial_state(
        loop_id, "issue-loop", "wronghash", str(repo), "main", "implementation"
    )
    state.status = "running"
    lc._write_state(state, str(repo))
    lock = lc.acquire_lock(loop_id, str(repo), "owner", 3600, host="foreign-host")
    assert lock is not None

    proc = _run_cli(
        [
            "propose",
            "--loop-id",
            loop_id,
            "--lease-token",
            lock.lease_token,
            "--project",
            str(repo),
        ]
    )
    response = _payload(proc)

    assert response["action"] == lc.Action.STOP.value
    assert response["params"]["repo_identity_verified"] is False


def test_push_guard_stop_after_complete_is_visible_on_next_propose(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    state = lc.load_state("abcd1234-issue-1", str(repo))
    state.pending_action = lc.PendingAction(
        "act-advance", lc.Action.ADVANCE_PHASE.value, "implementation", 1, lc.now_iso()
    )
    state.last_check_result = {"next_phase": "review"}
    state.state_version = 1
    lc._write_state(state, str(repo))

    completed = _run_cli(
        [
            "complete",
            "--loop-id",
            "abcd1234-issue-1",
            "--action-id",
            "act-advance",
            "--state-version",
            "1",
            "--result",
            '{"push_guard":{"branch_ok":false,"repo_identity_ok":true,"reason":"default_branch"}}',
            "--lease-token",
            lock.lease_token,
            "--project",
            str(repo),
        ]
    )
    assert completed.returncode == 0, completed.stderr
    assert lc.load_state("abcd1234-issue-1", str(repo)).status == "stopped"

    proposed = _run_cli(
        [
            "propose",
            "--loop-id",
            "abcd1234-issue-1",
            "--lease-token",
            lock.lease_token,
            "--project",
            str(repo),
        ]
    )
    payload = _payload(proposed)

    assert proposed.returncode == 0, proposed.stderr
    assert payload["action"] == lc.Action.STOP.value
    assert payload["params"]["stop_reason"] == "push_guard_violation"


def test_generic_safety_stop_reason_is_preserved(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    state = lc.load_state("abcd1234-issue-1", str(repo))
    state.status = "stopped"
    state.stop_reason = "safety_stop"
    lc._write_state(state, str(repo))

    payload = _propose(repo, "abcd1234-issue-1", lock.lease_token)

    assert payload["action"] == lc.Action.STOP.value
    assert payload["params"]["stop_reason"] == "safety_stop"


def test_repo_identity_push_guard_stop_includes_stop_reason(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = _write_running_state(repo)
    state = lc.load_state("abcd1234-issue-1", str(repo))
    state.pending_action = lc.PendingAction(
        "act-advance", lc.Action.ADVANCE_PHASE.value, "implementation", 1, lc.now_iso()
    )
    state.last_check_result = {"next_phase": "review"}
    state.state_version = 1
    lc._write_state(state, str(repo))

    completed = _run_cli(
        [
            "complete",
            "--loop-id",
            "abcd1234-issue-1",
            "--action-id",
            "act-advance",
            "--state-version",
            "1",
            "--result",
            '{"push_guard":{"branch_ok":true,"repo_identity_ok":false,"reason":"repo_identity_mismatch"}}',
            "--lease-token",
            lock.lease_token,
            "--project",
            str(repo),
        ]
    )
    assert completed.returncode == 0, completed.stderr

    payload = _propose(repo, "abcd1234-issue-1", lock.lease_token)

    assert payload["action"] == lc.Action.STOP.value
    assert payload["params"]["stop_reason"] == "repo_identity_mismatch"


def test_start_rolls_back_created_worktree_when_core_start_fails(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree_path = tmp_path / "loop-issue-7"
    removed: list[tuple[str, int, bool]] = []

    monkeypatch.setattr(loop_step.wm, "compute_loop_id", lambda _project, _issue: "loop-7")
    monkeypatch.setattr(loop_step.lc, "state_path", lambda _loop_id, _project: tmp_path / "state")
    monkeypatch.setattr(
        loop_step.wm, "worktree_path_for", lambda _project, _issue: str(worktree_path)
    )
    fake_lock = loop_step.lc.LockInfo(
        "owner", 1, "local", loop_step.lc.now_iso(), loop_step.lc.now_iso(), 3600, "lease"
    )
    monkeypatch.setattr(loop_step.lc, "acquire_lock", lambda *_args, **_kwargs: fake_lock)
    monkeypatch.setattr(loop_step.lc, "release_lock", lambda *_args, **_kwargs: True)

    def create_worktree(_project: str, _issue: int) -> Any:
        worktree_path.mkdir()
        return loop_step.wm.WorktreeInfo(
            path=str(worktree_path),
            branch="loop/issue-7",
            repo_identity_hash="abcd1234",
        )

    def fail_start(**_kwargs: Any) -> Any:
        raise loop_step.lc.InvalidStateError("start failed")

    def remove_worktree(project: str, issue: int, force: bool = False) -> None:
        removed.append((project, issue, force))

    monkeypatch.setattr(loop_step.wm, "create_worktree", create_worktree)
    monkeypatch.setattr(loop_step.lc, "start", fail_start)
    monkeypatch.setattr(loop_step.wm, "remove_worktree", remove_worktree)

    exit_code = loop_step.main(["start", "--issue", "7", "--project", str(repo)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out)["error"]["code"] == "invalid_state"
    assert removed == [(str(repo.resolve()), 7, True)]


def test_start_does_not_create_worktree_when_lease_is_unavailable(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    created: list[int] = []

    monkeypatch.setattr(loop_step.wm, "compute_loop_id", lambda _project, _issue: "loop-7")
    monkeypatch.setattr(loop_step.lc, "state_path", lambda _loop_id, _project: tmp_path / "state")
    monkeypatch.setattr(
        loop_step.wm, "worktree_path_for", lambda _project, _issue: str(tmp_path / "loop-issue-7")
    )
    monkeypatch.setattr(loop_step.lc, "acquire_lock", lambda *_args, **_kwargs: None)

    def create_worktree(_project: str, issue: int) -> Any:
        created.append(issue)
        raise AssertionError("worktree creation should not run without a lease")

    monkeypatch.setattr(loop_step.wm, "create_worktree", create_worktree)

    exit_code = loop_step.main(["start", "--issue", "7", "--project", str(repo)])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert json.loads(captured.out)["error"]["code"] == "lock_unavailable"
    assert created == []


def test_start_does_not_rollback_when_state_race_loses(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree_path = tmp_path / "loop-issue-7"
    removed: list[tuple[str, int, bool]] = []

    monkeypatch.setattr(loop_step.wm, "compute_loop_id", lambda _project, _issue: "loop-7")
    monkeypatch.setattr(loop_step.lc, "state_path", lambda _loop_id, _project: tmp_path / "state")
    monkeypatch.setattr(
        loop_step.wm, "worktree_path_for", lambda _project, _issue: str(worktree_path)
    )
    fake_lock = loop_step.lc.LockInfo(
        "owner", 1, "local", loop_step.lc.now_iso(), loop_step.lc.now_iso(), 3600, "lease"
    )
    monkeypatch.setattr(loop_step.lc, "acquire_lock", lambda *_args, **_kwargs: fake_lock)
    monkeypatch.setattr(loop_step.lc, "release_lock", lambda *_args, **_kwargs: True)

    def create_worktree(_project: str, _issue: int) -> Any:
        worktree_path.mkdir()
        return loop_step.wm.WorktreeInfo(
            path=str(worktree_path),
            branch="loop/issue-7",
            repo_identity_hash="abcd1234",
        )

    def fail_start(**_kwargs: Any) -> Any:
        raise loop_step.lc.InvalidStateError("state already exists: loop-7")

    def remove_worktree(project: str, issue: int, force: bool = False) -> None:
        removed.append((project, issue, force))

    monkeypatch.setattr(loop_step.wm, "create_worktree", create_worktree)
    monkeypatch.setattr(loop_step.lc, "start", fail_start)
    monkeypatch.setattr(loop_step.wm, "remove_worktree", remove_worktree)

    exit_code = loop_step.main(["start", "--issue", "7", "--project", str(repo)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out)["error"]["code"] == "already_exists"
    assert removed == []
