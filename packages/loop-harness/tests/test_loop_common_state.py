"""State machine, journal, and two-phase tests for loop_common."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.module_loader import load_module

lc = load_module("loop_common_state", "packages/loop-harness/lib/loop_common.py")


def _setup_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str = "pending"
) -> tuple[str, lc.LockInfo]:
    monkeypatch.setattr(lc, "resolve_root_worktree", lambda _project_dir: tmp_path)
    monkeypatch.setattr(lc.socket, "gethostname", lambda: "local")
    project_dir = str(tmp_path)
    loop_id = "abcd1234-issue-1"
    state = lc._initial_state(
        loop_id,
        "issue-loop",
        lc._repo_identity_hash(project_dir),
        project_dir,
        lc._current_branch(project_dir),
        "implementation",
    )
    state.status = status
    lc._write_state(state, project_dir)
    lock = lc.acquire_lock(loop_id, project_dir, "owner", 3600, host="local")
    assert lock is not None
    return project_dir, lock


def _check_result(
    passed: bool,
    signature: str = "sig",
    infra: bool = False,
    metadata: dict[str, object] | None = None,
) -> dict:
    mechanical = lc.CheckResult(passed, "mechanical", signature, [], "mechanical.json", infra)
    llm_review = lc.CheckResult(
        not infra,
        "llm_review",
        lc.compute_llm_review_signature([]),
        [],
        "review.json",
        infra,
    )
    combined = lc.combine_check_results(
        [mechanical, llm_review],
        {"critical": 0, "high": 0},
        frozenset({"mechanical", "llm_review"}),
    )
    result = lc.PhaseCheckResult(
        passed=combined.passed,
        results=combined.results,
        signature=combined.signature,
        infrastructure_failure=combined.infrastructure_failure,
        metadata={"reviewers": ["code-reviewer"], **(metadata or {})},
    )
    return lc.phase_check_to_dict(result)


def _maker_result(agent: str = "backend-python-dev") -> dict:
    return {"maker": {"agent": agent, "tool": "codex"}}


def test_state_to_dict_does_not_mutate_missing_phase_counter() -> None:
    state = lc._initial_state(
        "abcd1234-issue-1",
        "issue-loop",
        "abcd1234",
        "/tmp/wt",
        "loop/issue-1",
        "implementation",
    )
    state.guards = {}

    data = lc._state_to_dict(state)

    assert data["iteration"] == 0
    assert state.guards == {}


def test_maker_agent_state_roundtrip_and_legacy_compatibility() -> None:
    state = lc._initial_state(
        "abcd1234-issue-1",
        "issue-loop",
        "abcd1234",
        "/tmp/wt",
        "loop/issue-1",
        "implementation",
    )
    state.maker_agent = "backend-python-dev"

    data = lc._state_to_dict(state)
    assert lc._state_from_dict(data).maker_agent == "backend-python-dev"

    data.pop("maker_agent")
    assert lc._state_from_dict(data).maker_agent is None


def test_first_completed_maker_is_persisted_and_later_result_must_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        proposal.action_id,
        proposal.state_version,
        _maker_result(),
        lock.lease_token,
    )
    state = lc.load_state("abcd1234-issue-1", project_dir)

    assert state.maker_agent == "backend-python-dev"

    with pytest.raises(lc.ProtocolViolationError, match="maker agent mismatch"):
        lc.apply_action_effect(
            state,
            lc.Action.RUN_MAKER.value,
            {"maker": {"agent": "requirements", "tool": "claude-direct"}},
            project_dir,
        )
    assert state.maker_agent == "backend-python-dev"


def test_maker_agent_is_reused_in_pr_review_response_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, _lock = _setup_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.phase = "pr_review_response"
    state.guards[state.phase] = lc.GuardCounters()
    state.maker_agent = "backend-python-dev"

    params = lc._proposal_params(state, lc.Action.RUN_MAKER.value, project_dir)

    assert params["maker_agent"] == "backend-python-dev"


def test_unselected_maker_proposal_keeps_definition_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, _lock = _setup_loop(tmp_path, monkeypatch)
    state = lc.load_state("abcd1234-issue-1", project_dir)

    params = lc._proposal_params(state, lc.Action.RUN_MAKER.value, project_dir)

    assert params["maker_agent"] == "auto"


def test_custom_loop_proposal_keeps_phase_specific_maker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = lc._initial_state(
        "custom-loop-id",
        "custom-loop",
        "abcd1234",
        str(tmp_path),
        "feature/custom",
        "verification",
    )
    state.maker_agent = "backend-python-dev"
    monkeypatch.setattr(
        lc,
        "_load_phase_definition",
        lambda _state, _project: {"maker": {"agent": "tester"}},
    )

    params = lc._proposal_params(state, lc.Action.RUN_MAKER.value, str(tmp_path))

    assert params["maker_agent"] == "tester"


def test_exit_success_proposal_params_include_non_blocking_open_from_last_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """issue #213/B: `exit_success` proposal params must mirror the last completed phase
    check's `non_blocking_open` metadata (see `pr_review_wait.phase_check_from_review_findings`)
    so LP-1's skill-facing CLI response can report residual non-blocking findings too, not
    just LP-2's own Issue comment."""
    project_dir, _lock = _setup_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pr_number = 77
    state.last_check_result = {
        "passed": True,
        "signature": "sig",
        "infrastructure_failure": False,
        "results": [],
        "metadata": {
            "non_blocking_open": [
                {
                    "signature": "sig-low",
                    "severity": "low",
                    "path": "app.py",
                    "line": 10,
                    "body_excerpt": "[P4] optional",
                }
            ]
        },
    }

    params = lc._proposal_params(state, lc.Action.EXIT_SUCCESS.value, project_dir)

    assert params["pr_number"] == 77
    assert params["non_blocking_open"] == [
        {
            "signature": "sig-low",
            "severity": "low",
            "path": "app.py",
            "line": 10,
            "body_excerpt": "[P4] optional",
        }
    ]


def test_exit_success_proposal_params_non_blocking_open_defaults_to_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phases without pr_review metadata (e.g. a plain `implementation`-phase success) must
    not error and must report an empty list, matching prior behavior exactly."""
    project_dir, _lock = _setup_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pr_number = 77
    state.last_check_result = {
        "passed": True,
        "signature": "sig",
        "infrastructure_failure": False,
        "results": [],
    }

    params = lc._proposal_params(state, lc.Action.EXIT_SUCCESS.value, project_dir)

    assert params == {"pr_number": 77, "non_blocking_open": []}


def test_custom_loop_complete_accepts_non_allowlisted_maker_without_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.definition_id = "custom-loop"
    lc._write_state(state, project_dir)

    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        proposal.action_id,
        proposal.state_version,
        {"maker": {"agent": "custom-maker", "tool": "claude-direct"}},
        lock.lease_token,
    )

    completed = lc.load_state("abcd1234-issue-1", project_dir)
    assert completed.status == "running"
    assert completed.maker_agent is None


def test_custom_loop_reconcile_accepts_non_allowlisted_maker_without_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.definition_id = "custom-loop"
    lc._write_state(state, project_dir)
    lc.append_journal_event(
        "abcd1234-issue-1",
        project_dir,
        "completed",
        "maker",
        proposal.action_id,
        {
            "action": lc.Action.RUN_MAKER.value,
            "result": {"maker": {"agent": "custom-maker", "tool": "claude-direct"}},
        },
    )

    outcome = lc.reconcile("abcd1234-issue-1", project_dir, lock.lease_token)

    completed = lc.load_state("abcd1234-issue-1", project_dir)
    assert outcome.action_taken == "resolved_from_journal"
    assert completed.maker_agent is None


def test_completed_maker_rejects_agent_outside_allowlist_before_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)

    with pytest.raises(lc.ProtocolViolationError, match="maker agent is not allowed"):
        lc.complete(
            "abcd1234-issue-1",
            project_dir,
            proposal.action_id,
            proposal.state_version,
            {"maker": {"agent": "requirements", "tool": "claude-direct"}},
            lock.lease_token,
        )

    assert (
        lc.find_journal_event("abcd1234-issue-1", project_dir, proposal.action_id, "completed")
        is None
    )


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"maker": {}},
        {"maker": {"agent": ""}},
        {"maker": {"agent": 123}},
    ],
)
def test_completed_maker_requires_non_empty_agent_before_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: dict
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)

    with pytest.raises(lc.ProtocolViolationError, match="maker"):
        lc.complete(
            "abcd1234-issue-1",
            project_dir,
            proposal.action_id,
            proposal.state_version,
            result,
            lock.lease_token,
        )

    assert (
        lc.find_journal_event("abcd1234-issue-1", project_dir, proposal.action_id, "completed")
        is None
    )


def test_completed_maker_rejects_agent_mismatch_before_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.maker_agent = "backend-python-dev"
    state.pending_action = lc.PendingAction(
        "act-maker-2", lc.Action.RUN_MAKER.value, "implementation", 2, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, project_dir)

    with pytest.raises(lc.ProtocolViolationError, match="maker agent mismatch"):
        lc.complete(
            "abcd1234-issue-1",
            project_dir,
            "act-maker-2",
            1,
            _maker_result("requirements"),
            lock.lease_token,
        )

    assert (
        lc.find_journal_event("abcd1234-issue-1", project_dir, "act-maker-2", "completed") is None
    )


def test_reconcile_completed_maker_persists_selected_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    lc.append_journal_event(
        "abcd1234-issue-1",
        project_dir,
        "completed",
        "maker",
        proposal.action_id,
        {
            "action": lc.Action.RUN_MAKER.value,
            "result": {"maker": {"agent": "backend-python-dev", "tool": "codex"}},
        },
    )

    lc.reconcile("abcd1234-issue-1", project_dir, lock.lease_token)

    assert lc.load_state("abcd1234-issue-1", project_dir).maker_agent == "backend-python-dev"


@pytest.mark.parametrize(
    ("stored_agent", "result", "error"),
    [
        (None, {"maker": {}}, "maker agent must be a non-empty string"),
        ("backend-python-dev", _maker_result("requirements"), "maker agent mismatch"),
    ],
)
def test_reconcile_completed_maker_rejects_malformed_or_mismatched_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stored_agent: str | None,
    result: dict,
    error: str,
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.maker_agent = stored_agent
    lc._write_state(state, project_dir)
    lc.append_journal_event(
        "abcd1234-issue-1",
        project_dir,
        "completed",
        "maker",
        proposal.action_id,
        {"action": lc.Action.RUN_MAKER.value, "result": result},
    )

    with pytest.raises(lc.ProtocolViolationError, match=error):
        lc.reconcile("abcd1234-issue-1", project_dir, lock.lease_token)

    assert lc.load_state("abcd1234-issue-1", project_dir).pending_action is not None


def test_status_transitions_and_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    assert proposal.action == lc.Action.RUN_MAKER.value
    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        proposal.action_id,
        proposal.state_version,
        _maker_result(),
        lock.lease_token,
    )
    assert lc.load_state("abcd1234-issue-1", project_dir).status == "running"

    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    assert proposal.action == lc.Action.RUN_CHECKER.value
    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        proposal.action_id,
        proposal.state_version,
        {"check_result": _check_result(True)},
        lock.lease_token,
    )
    checked = lc.load_state("abcd1234-issue-1", project_dir)
    assert checked.status == "running"
    assert checked.last_check_result["next_phase"] == "pr_review_response"

    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.status = "failed"
    lc._write_state(state, project_dir)
    resumed = lc.resume("abcd1234-issue-1", project_dir, True, "owner-2", 3600)
    assert resumed.state.status == "running"
    assert resumed.lease_token != lock.lease_token


def test_complete_accepts_raw_passed_checker_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    maker = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        maker.action_id,
        maker.state_version,
        _maker_result(),
        lock.lease_token,
    )
    checker = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)

    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        checker.action_id,
        checker.state_version,
        _check_result(True),
        lock.lease_token,
    )

    event = lc.find_journal_event("abcd1234-issue-1", project_dir, checker.action_id, "completed")
    checked = lc.load_state("abcd1234-issue-1", project_dir)
    assert checked.status == "running"
    assert checked.last_check_result["next_phase"] == "pr_review_response"
    assert event["payload"]["check_result"]["passed"] is True


def test_state_version_increments_and_heartbeat_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    assert proposal.state_version == 1
    assert lc.heartbeat("abcd1234-issue-1", project_dir, lock.lease_token) is True
    assert lc.load_state("abcd1234-issue-1", project_dir).state_version == 1
    result = lc.complete(
        "abcd1234-issue-1",
        project_dir,
        proposal.action_id,
        proposal.state_version,
        _maker_result(),
        lock.lease_token,
    )
    assert result.state_version == 2


def test_complete_rejects_stale_action_and_replays_same_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    with pytest.raises(lc.StaleActionError):
        lc.complete(
            "abcd1234-issue-1", project_dir, "other", proposal.state_version, {}, lock.lease_token
        )
    result = lc.complete(
        "abcd1234-issue-1",
        project_dir,
        proposal.action_id,
        proposal.state_version,
        _maker_result(),
        lock.lease_token,
    )
    replay = lc.complete(
        "abcd1234-issue-1",
        project_dir,
        proposal.action_id,
        proposal.state_version,
        {},
        lock.lease_token,
    )
    assert replay.idempotent_replay is True
    assert replay.state_version == result.state_version


def test_second_propose_before_complete_is_protocol_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    with pytest.raises(lc.ProtocolViolationError):
        lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)


def test_complete_appends_journal_before_state_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("crash between journal and state")

    monkeypatch.setattr(lc, "_write_state", fail_write)
    with pytest.raises(OSError):
        lc.complete(
            "abcd1234-issue-1",
            project_dir,
            proposal.action_id,
            proposal.state_version,
            _maker_result(),
            lock.lease_token,
        )
    event = lc.find_journal_event("abcd1234-issue-1", project_dir, proposal.action_id, "completed")
    assert event is not None
    with open(lc.state_path("abcd1234-issue-1", project_dir), encoding="utf-8") as f:
        assert json.load(f)["pending_action"]["action_id"] == proposal.action_id


def test_reconcile_resolves_from_completed_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    lc.append_journal_event(
        "abcd1234-issue-1",
        project_dir,
        "completed",
        "maker",
        proposal.action_id,
        {"action": lc.Action.RUN_MAKER.value, "result": {}},
    )
    outcome = lc.reconcile("abcd1234-issue-1", project_dir, lock.lease_token)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    assert outcome.action_taken == "resolved_from_journal"
    assert state.pending_action is None
    assert state.status == "running"
    assert state.maker_agent is None


def test_reconcile_completed_maker_sets_last_completed_action_for_next_propose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    lc.append_journal_event(
        "abcd1234-issue-1",
        project_dir,
        "completed",
        "maker",
        proposal.action_id,
        {"action": lc.Action.RUN_MAKER.value, "result": _maker_result()},
    )

    lc.reconcile("abcd1234-issue-1", project_dir, lock.lease_token)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    next_proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)

    assert state.last_completed_action.action_id == proposal.action_id
    assert next_proposal.action == lc.Action.RUN_CHECKER.value


def test_reconcile_resolves_checker_from_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        "act-check", lc.Action.RUN_CHECKER.value, "implementation", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, project_dir)
    lc.save_artifact(
        "abcd1234-issue-1",
        project_dir,
        "act-check",
        "check_result.json",
        json.dumps(_check_result(False)),
    )
    outcome = lc.reconcile("abcd1234-issue-1", project_dir, lock.lease_token)
    assert outcome.action_taken == "resolved_from_artifact"
    assert lc.load_state("abcd1234-issue-1", project_dir).pending_action is None


def test_reconcile_rejects_legacy_empty_checker_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        "act-check", lc.Action.RUN_CHECKER.value, "implementation", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, project_dir)
    lc.save_artifact(
        "abcd1234-issue-1",
        project_dir,
        "act-check",
        "check_result.json",
        json.dumps(
            {
                "passed": True,
                "results": [],
                "signature": "",
                "infrastructure_failure": False,
            }
        ),
    )

    with pytest.raises(lc.IntegrityError, match="sealed checker"):
        lc.reconcile("abcd1234-issue-1", project_dir, lock.lease_token)

    assert lc.load_state("abcd1234-issue-1", project_dir).pending_action is not None


@pytest.mark.parametrize("invalid_kind", ["missing_code_reviewer", "duplicate", "source"])
def test_complete_rejects_invalid_checker_reviewer_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_kind: str,
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    maker = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        maker.action_id,
        maker.state_version,
        _maker_result(),
        lock.lease_token,
    )
    checker = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    result = _check_result(False)
    if invalid_kind == "missing_code_reviewer":
        result["metadata"]["reviewers"] = ["security-reviewer"]
    elif invalid_kind == "duplicate":
        result["metadata"]["reviewers"] = ["code-reviewer", "code-reviewer"]
    else:
        result["results"][1]["findings"] = [
            {
                "severity": "high",
                "summary": "must fix",
                "source": "unbound-reviewer",
                "path": "app.py",
                "line": 1,
            }
        ]

    with pytest.raises(lc.ProtocolViolationError, match="sealed checker"):
        lc.complete(
            "abcd1234-issue-1",
            project_dir,
            checker.action_id,
            checker.state_version,
            result,
            lock.lease_token,
        )

    assert (
        lc.find_journal_event("abcd1234-issue-1", project_dir, checker.action_id, "completed")
        is None
    )


@pytest.mark.parametrize(
    "invalid_kind",
    [
        "top_passed",
        "top_infrastructure",
        "top_signature",
        "llm_passed",
        "llm_signature",
        "infra_passed",
        "mechanical_findings",
    ],
)
def test_complete_rejects_semantically_inconsistent_checker_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_kind: str,
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    maker = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        maker.action_id,
        maker.state_version,
        _maker_result(),
        lock.lease_token,
    )
    checker = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    result = _check_result(True)
    if invalid_kind == "top_passed":
        result["passed"] = False
    elif invalid_kind == "top_infrastructure":
        result["infrastructure_failure"] = True
    elif invalid_kind == "top_signature":
        result["signature"] = "forged"
    elif invalid_kind == "llm_passed":
        result["results"][1]["passed"] = False
    elif invalid_kind == "llm_signature":
        result["results"][1]["signature"] = "forged"
    elif invalid_kind == "infra_passed":
        result["results"][1]["infrastructure_failure"] = True
    else:
        result["results"][0]["findings"] = [
            {
                "severity": "high",
                "summary": "unexpected mechanical finding",
                "source": "mechanical",
                "path": None,
                "line": None,
            }
        ]

    with pytest.raises(lc.ProtocolViolationError, match="sealed checker"):
        lc.complete(
            "abcd1234-issue-1",
            project_dir,
            checker.action_id,
            checker.state_version,
            result,
            lock.lease_token,
        )


def test_reconcile_rejects_semantically_inconsistent_checker_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        "act-check", lc.Action.RUN_CHECKER.value, "implementation", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, project_dir)
    result = _check_result(True)
    result["passed"] = False
    lc.save_artifact(
        "abcd1234-issue-1",
        project_dir,
        "act-check",
        "check_result.json",
        json.dumps(result),
    )

    with pytest.raises(lc.IntegrityError, match="sealed checker"):
        lc.reconcile("abcd1234-issue-1", project_dir, lock.lease_token)


def test_reconcile_rerun_required_for_checker_without_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        "act-check", lc.Action.RUN_CHECKER.value, "implementation", 1, lc.now_iso()
    )
    lc._write_state(state, project_dir)
    outcome = lc.reconcile("abcd1234-issue-1", project_dir, lock.lease_token)
    assert outcome.action_taken == "rerun_required"
    assert lc.load_state("abcd1234-issue-1", project_dir).pending_action is not None


def test_reconcile_marks_unresolved_maker_as_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    outcome = lc.reconcile("abcd1234-issue-1", project_dir, lock.lease_token)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    assert outcome.action_taken == "marked_infrastructure_failure"
    assert state.last_check_result["infrastructure_failure"] is True
    assert state.pending_action is None


def test_passed_transition_verifies_journal_digest_only_for_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, _lock = _setup_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    monkeypatch.setattr(
        lc,
        "_load_phase_definition",
        lambda _state, _project: {
            "on_success": {"disposition": lc.Action.EXIT_SUCCESS.value},
            "on_failure": {"disposition": lc.Action.EXIT_FAILURE.value},
        },
    )
    monkeypatch.setattr(lc, "_load_loop_config", lambda _project: {})
    lc.append_journal_event(
        "abcd1234-issue-1",
        project_dir,
        "completed",
        "checker",
        "act-check",
        {"action": lc.Action.RUN_CHECKER.value, "check_result": _check_result(False, "old")},
    )
    with pytest.raises(lc.IntegrityError):
        lc.apply_action_effect(
            state,
            lc.Action.RUN_CHECKER.value,
            {"check_result": _check_result(True, "new")},
            project_dir,
            "abcd1234-issue-1",
            "act-check",
        )

    lc.apply_action_effect(
        state,
        lc.Action.RUN_CHECKER.value,
        {"check_result": _check_result(False, "new")},
        project_dir,
        "abcd1234-issue-1",
        "act-check",
    )
    assert state.status == "running"


def test_checker_success_proposes_and_completes_advance_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    maker = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        maker.action_id,
        maker.state_version,
        _maker_result(),
        lock.lease_token,
    )
    checker = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        checker.action_id,
        checker.state_version,
        {"check_result": _check_result(True)},
        lock.lease_token,
    )
    state = lc.load_state("abcd1234-issue-1", project_dir)
    advance = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        advance.action_id,
        advance.state_version,
        {"pr_number": 123},
        lock.lease_token,
    )
    advanced = lc.load_state("abcd1234-issue-1", project_dir)

    assert state.phase == "implementation"
    assert state.last_check_result["next_phase"] == "pr_review_response"
    assert advance.action == lc.Action.ADVANCE_PHASE.value
    assert advanced.phase == "pr_review_response"
    assert "pr_review_response" in advanced.guards


def test_reconcile_passed_checker_artifact_uses_durable_phase_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        "act-check", lc.Action.RUN_CHECKER.value, "implementation", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, project_dir)
    lc.save_artifact(
        "abcd1234-issue-1",
        project_dir,
        "act-check",
        "check_result.json",
        json.dumps(_check_result(True)),
    )

    outcome = lc.reconcile("abcd1234-issue-1", project_dir, lock.lease_token)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)

    assert outcome.action_taken == "resolved_from_artifact"
    assert proposal.action == lc.Action.ADVANCE_PHASE.value
    assert proposal.context["params"]["next_phase"] == "pr_review_response"


def test_advance_phase_push_guard_stop_does_not_change_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    maker = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        maker.action_id,
        maker.state_version,
        _maker_result(),
        lock.lease_token,
    )
    checker = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    phase_def = {
        "on_success": {"disposition": "advance_phase", "next": "review"},
        "on_failure": {"disposition": "exit_failure"},
    }
    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        checker.action_id,
        checker.state_version,
        {"check_result": _check_result(True), "phase_def": phase_def},
        lock.lease_token,
    )
    advance = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        advance.action_id,
        advance.state_version,
        {"push_guard": {"branch_ok": False, "repo_identity_ok": True, "reason": "default_branch"}},
        lock.lease_token,
    )
    stopped = lc.load_state("abcd1234-issue-1", project_dir)

    assert advance.action == lc.Action.ADVANCE_PHASE.value
    assert stopped.phase == "implementation"
    assert stopped.status == "stopped"


def test_push_guard_violation_transitions_to_stopped() -> None:
    state = lc._initial_state(
        "loop", "issue-loop", "hash", "/tmp/wt", "loop/issue-1", "implementation"
    )
    lc.apply_action_effect(
        state,
        lc.Action.ADVANCE_PHASE.value,
        {"push_guard": {"branch_ok": False, "repo_identity_ok": True, "reason": "default_branch"}},
    )
    assert state.status == "stopped"
    assert state.stop_reason == "push_guard_violation"


def test_repo_identity_guard_violation_transitions_to_stopped() -> None:
    state = lc._initial_state(
        "loop", "issue-loop", "hash", "/tmp/wt", "loop/issue-1", "implementation"
    )
    lc.apply_action_effect(
        state,
        lc.Action.ADVANCE_PHASE.value,
        {
            "push_guard": {
                "branch_ok": True,
                "repo_identity_ok": False,
                "reason": "repo_identity_mismatch",
            }
        },
    )
    assert state.status == "stopped"
    assert state.stop_reason == "repo_identity_mismatch"


def test_proposal_context_does_not_let_params_override_reserved_keys() -> None:
    params = {
        "params": "nested collision",
        "reason": "spoofed reason",
        "lease_token": "spoofed-token",
        "mechanical": {"commands": ["pytest -q"]},
    }

    context = lc._proposal_context(params)

    assert context["params"] == params
    assert context["mechanical"] == {"commands": ["pytest -q"]}
    assert context.get("reason") is None
    assert context.get("lease_token") is None


def test_wait_external_review_params_include_config_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = lc._initial_state(
        "loop", "issue-loop", "hash", "/tmp/wt", "loop/issue-1", "pr_review_response"
    )
    state.pr_number = 123
    monkeypatch.setattr(
        lc,
        "_load_phase_definition",
        lambda _state, _project: {"checker": {"external_signal": {"source": "github"}}},
    )
    monkeypatch.setattr(
        lc,
        "_load_loop_config",
        lambda _project: {"pr_review": {"poll_interval_seconds": 77, "timeout_seconds": 88}},
    )

    params = lc._proposal_params(state, lc.Action.WAIT_EXTERNAL_REVIEW.value, str(tmp_path))

    assert params == {
        "source": "github",
        "poll_interval_seconds": 77,
        "timeout_seconds": 88,
        "pr_number": 123,
        "push_required": False,
        "verified_branch": "loop/issue-1",
    }


def test_wait_external_review_completion_applies_checker_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch, status="waiting_external")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.phase = "pr_review_response"
    state.pr_number = 123
    state.guards["pr_review_response"] = lc.GuardCounters(
        iteration=2,
        no_progress_streak=1,
        last_signature="stale",
        infrastructure_failure_count=1,
    )
    state.pending_action = lc.PendingAction(
        "act-wait",
        lc.Action.WAIT_EXTERNAL_REVIEW.value,
        "pr_review_response",
        1,
        lc.now_iso(),
    )
    state.state_version = 1
    lc._write_state(state, project_dir)

    result = lc.complete(
        "abcd1234-issue-1",
        project_dir,
        "act-wait",
        1,
        {
            "completed": True,
            "check_result": _check_result(
                True,
                "",
                metadata={"current_iteration_findings": {"signatures": [], "new_count": 0}},
            ),
        },
        lock.lease_token,
    )

    state = lc.load_state("abcd1234-issue-1", project_dir)
    assert result.ok is True
    assert state.status == "passed"
    assert state.last_check_result is not None
    assert state.last_check_result["passed"] is True
    assert state.last_check_result["metadata"] == {
        "current_iteration_findings": {"signatures": [], "new_count": 0},
        "reviewers": ["code-reviewer"],
    }
    counters = state.guards["pr_review_response"]
    assert counters.no_progress_streak == 0
    assert counters.last_signature is None
    assert counters.infrastructure_failure_count == 0


def test_wait_reviewer_unavailable_completion_stops_without_changing_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch, status="waiting_external")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.phase = "pr_review_response"
    state.pr_number = 123
    state.guards["pr_review_response"] = lc.GuardCounters(
        iteration=2,
        no_progress_streak=1,
        last_signature="previous",
        infrastructure_failure_count=1,
    )
    state.pending_action = lc.PendingAction(
        "act-wait",
        lc.Action.WAIT_EXTERNAL_REVIEW.value,
        "pr_review_response",
        2,
        lc.now_iso(),
    )
    state.state_version = 1
    lc._write_state(state, project_dir)
    check_result = lc.phase_check_to_dict(
        lc.PhaseCheckResult(
            passed=False,
            results=[],
            signature="external_reviewer_unavailable",
            infrastructure_failure=False,
            metadata={
                "reviewer_unavailable_comment_ids": ["issue_comment:30"],
                "reviewer_unavailable_reason": "rate_limited",
            },
        )
    )

    result = lc.complete(
        "abcd1234-issue-1",
        project_dir,
        "act-wait",
        1,
        {"completed": True, "check_result": check_result},
        lock.lease_token,
    )

    stopped = lc.load_state("abcd1234-issue-1", project_dir)
    assert result.ok is True
    assert stopped.status == "stopped"
    assert stopped.stop_reason == "external_reviewer_unavailable"
    assert stopped.pr_review["processed_comment_ids"] == ["issue_comment:30"]
    assert stopped.guards["pr_review_response"] == lc.GuardCounters(
        iteration=2,
        no_progress_streak=1,
        last_signature="previous",
        infrastructure_failure_count=1,
    )
    assert lc._proposal_params(stopped, lc.Action.STOP.value, project_dir) == {
        "stop_reason": "external_reviewer_unavailable",
        "pr_number": 123,
    }


def test_reviewer_unavailable_processed_comment_survives_stop_completion_and_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch, status="waiting_external")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.phase = "pr_review_response"
    state.pr_number = 123
    state.guards["pr_review_response"] = lc.GuardCounters()
    state.pending_action = lc.PendingAction(
        "act-wait",
        lc.Action.WAIT_EXTERNAL_REVIEW.value,
        "pr_review_response",
        1,
        lc.now_iso(),
    )
    state.state_version = 1
    lc._write_state(state, project_dir)
    check_result = lc.phase_check_to_dict(
        lc.PhaseCheckResult(
            passed=False,
            results=[],
            signature="external_reviewer_unavailable",
            infrastructure_failure=False,
            metadata={
                "reviewer_unavailable_comment_ids": ["issue_comment:30"],
                "reviewer_unavailable_reason": "rate_limited",
            },
        )
    )
    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        "act-wait",
        1,
        {"completed": True, "check_result": check_result},
        lock.lease_token,
    )
    stop = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        stop.action_id,
        stop.state_version,
        {},
        lock.lease_token,
    )

    resumed = lc.resume("abcd1234-issue-1", project_dir, True, "resume-owner", 3600)
    wait = lc.propose("abcd1234-issue-1", project_dir, resumed.lease_token)
    resumed_state = lc.load_state("abcd1234-issue-1", project_dir)

    assert stop.action == lc.Action.STOP.value
    assert wait.action == lc.Action.WAIT_EXTERNAL_REVIEW.value
    assert resumed_state.pr_review["processed_comment_ids"] == ["issue_comment:30"]


def test_advance_phase_persists_pr_number() -> None:
    state = lc._initial_state(
        "loop", "issue-loop", "hash", "/tmp/wt", "loop/issue-1", "implementation"
    )
    state.last_check_result = {"next_phase": "pr_review_response"}

    lc.apply_action_effect(
        state,
        lc.Action.ADVANCE_PHASE.value,
        {"pr_number": "123"},
    )

    assert state.phase == "pr_review_response"
    assert state.pr_number == 123


def test_complete_stop_preserves_existing_stop_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch, status="stopped")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.stop_reason = "repo_identity_mismatch"
    state.pending_action = lc.PendingAction(
        "act-stop", lc.Action.STOP.value, "implementation", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, project_dir)

    result = lc.complete(
        "abcd1234-issue-1",
        project_dir,
        "act-stop",
        1,
        {},
        lock.lease_token,
    )

    assert result.next_hint == "loop terminal"
    assert lc.load_state("abcd1234-issue-1", project_dir).stop_reason == "repo_identity_mismatch"


def test_invalid_pr_number_is_rejected_before_completed_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        "act-advance", lc.Action.ADVANCE_PHASE.value, "implementation", 1, lc.now_iso()
    )
    state.last_check_result = {"next_phase": "pr_review_response"}
    state.state_version = 1
    lc._write_state(state, project_dir)

    with pytest.raises(ValueError, match="pr_number"):
        lc.complete(
            "abcd1234-issue-1",
            project_dir,
            "act-advance",
            1,
            {"pr_number": "https://github.com/example/repo/pull/123"},
            lock.lease_token,
        )

    assert (
        lc.find_journal_event("abcd1234-issue-1", project_dir, "act-advance", "completed") is None
    )
    assert lc.load_state("abcd1234-issue-1", project_dir).pending_action is not None


def test_fractional_pr_number_is_rejected_before_completed_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        "act-advance", lc.Action.ADVANCE_PHASE.value, "implementation", 1, lc.now_iso()
    )
    state.last_check_result = {"next_phase": "pr_review_response"}
    state.state_version = 1
    lc._write_state(state, project_dir)

    with pytest.raises(ValueError, match="pr_number"):
        lc.complete(
            "abcd1234-issue-1",
            project_dir,
            "act-advance",
            1,
            {"pr_number": 123.9},
            lock.lease_token,
        )

    assert (
        lc.find_journal_event("abcd1234-issue-1", project_dir, "act-advance", "completed") is None
    )


def test_external_review_advance_requires_pr_number_before_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        "act-advance", lc.Action.ADVANCE_PHASE.value, "implementation", 1, lc.now_iso()
    )
    state.last_check_result = {"next_phase": "pr_review_response"}
    state.state_version = 1
    lc._write_state(state, project_dir)

    with pytest.raises(ValueError, match="pr_number"):
        lc.complete(
            "abcd1234-issue-1",
            project_dir,
            "act-advance",
            1,
            {},
            lock.lease_token,
        )

    assert (
        lc.find_journal_event("abcd1234-issue-1", project_dir, "act-advance", "completed") is None
    )


def test_artifact_is_redacted_and_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, _lock = _setup_loop(tmp_path, monkeypatch)
    rel = lc.save_artifact(
        "abcd1234-issue-1",
        project_dir,
        "act-1",
        "mechanical_0.log",
        "token=ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    path = lc.loop_dir("abcd1234-issue-1", project_dir) / rel
    assert "[REDACTED]" in path.read_text(encoding="utf-8")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_guarded_lease_section_yields_for_valid_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code review #3: a valid lease still lets the caller run its guarded writes."""
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    entered = False
    with lc.guarded_lease_section("abcd1234-issue-1", project_dir, lock.lease_token):
        entered = True
    assert entered


def test_guarded_lease_section_rejects_mismatched_lease_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code review #3: a stale/foreign lease token must fail closed before any write runs."""
    project_dir, _lock = _setup_loop(tmp_path, monkeypatch)
    entered = False
    with pytest.raises(lc.WriteRejectedError):
        with lc.guarded_lease_section("abcd1234-issue-1", project_dir, "not-the-real-token"):
            entered = True
    assert not entered


def test_guarded_lease_section_rejects_reacquired_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code review #3: a lease invalidated by a concurrent reacquire must be rejected too.

    Simulates the TOCTOU window the fix closes: worker A's token is still the one it was
    handed, but its heartbeat has since lapsed and worker B reacquired the lease, so A's
    in-hand token no longer matches lock.json (mirrors
    `test_old_lease_cannot_release_or_heartbeat_after_reacquire` in test_loop_common_lock.py).
    """
    project_dir, stale_lock = _setup_loop(tmp_path, monkeypatch)
    path = lc.lock_path("abcd1234-issue-1", project_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["heartbeat_at"] = "1970-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    reacquired = lc.reacquire_lease("abcd1234-issue-1", project_dir, "other-owner", 3600)
    assert reacquired.lease_token != stale_lock.lease_token
    with pytest.raises(lc.WriteRejectedError):
        with lc.guarded_lease_section("abcd1234-issue-1", project_dir, stale_lock.lease_token):
            pass


def test_guarded_lease_section_holds_lock_file_exclusively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code review #3: the section must hold the same flock `acquire_lock`/`reacquire_lease`
    use, so a concurrent lease (re)acquisition attempt blocks instead of racing in.
    """
    project_dir, lock = _setup_loop(tmp_path, monkeypatch)
    path = lc.lock_path("abcd1234-issue-1", project_dir)
    with lc.guarded_lease_section("abcd1234-issue-1", project_dir, lock.lease_token):
        probe_fd = os.open(path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe_fd)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


# --- Issue #196: LP-1 push-integrity warning ---------------------------------------------
#
# Uses a *real* git repo + a real bare `origin` remote (unlike the rest of this file's
# `_setup_loop`, which runs against a bare tmp dir and never actually calls `git ls-remote`)
# so `_remote_head()` exercises the real network-shaped path.


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    """Init a real, remote-less git repo (Issue #208 gitlink/config-tamper tests)."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "loop-harness@example.com"], path)
    _git(["config", "user.name", "Loop Harness Test"], path)
    (path / "README.md").write_text("root\n", encoding="utf-8")
    _git(["add", "README.md"], path)
    _git(["commit", "-m", "init"], path)


def _init_repo_with_remote(path: Path, remote_path: Path, branch: str) -> None:
    """Init a real repo + a real bare remote, without pushing (tests push explicitly)."""
    remote_path.mkdir(parents=True, exist_ok=True)
    _git(["init", "--bare", "-b", "main"], remote_path)
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", branch], path)
    _git(["config", "user.email", "loop-harness@example.com"], path)
    _git(["config", "user.name", "Loop Harness Test"], path)
    (path / "README.md").write_text("root\n", encoding="utf-8")
    _git(["add", "README.md"], path)
    _git(["commit", "-m", "init"], path)
    _git(["remote", "add", "origin", str(remote_path)], path)


def _setup_real_git_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str = "loop/issue-1",
    status: str = "running",
) -> tuple[str, lc.LockInfo]:
    """`_setup_loop`'s real-git counterpart: `project_dir` is a real repo with a real remote."""
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, branch)
    monkeypatch.setattr(lc, "resolve_root_worktree", lambda _project_dir: tmp_path)
    monkeypatch.setattr(lc.socket, "gethostname", lambda: "local")
    project_dir = str(repo)
    loop_id = "abcd1234-issue-1"
    state = lc._initial_state(
        loop_id,
        "issue-loop",
        lc._repo_identity_hash(project_dir),
        project_dir,
        branch,
        "implementation",
        # This whole section tests Issue #196's LP-1 push-integrity warning, which requires an
        # actual seeded baseline -- opt in explicitly (the default is `False`, see
        # `_initial_state()`'s own docstring for why LP-2 must not get this for free).
        precedent_push_check=True,
    )
    state.status = status
    lc._write_state(state, project_dir)
    lock = lc.acquire_lock(loop_id, project_dir, "owner", 3600, host="local")
    assert lock is not None
    return project_dir, lock


def test_remote_head_reads_real_remote(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")
    _git(["push", "origin", "loop/issue-1"], repo)
    expected = _git(["rev-parse", "HEAD"], repo)

    assert lc._remote_head(str(repo), "loop/issue-1") == expected


def test_remote_head_returns_absent_sentinel_for_unpushed_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")

    assert lc._remote_head(str(repo), "loop/issue-1") == lc.LP1_REMOTE_HEAD_ABSENT


def test_remote_head_returns_none_when_query_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")
    _git(["remote", "remove", "origin"], repo)

    assert lc._remote_head(str(repo), "loop/issue-1") is None


def test_detect_precedent_push_returns_none_without_baseline(tmp_path: Path) -> None:
    state = lc._initial_state("loop", "issue-loop", "hash", str(tmp_path), "main", "implementation")
    state.remote_head_baseline = None

    assert lc._detect_precedent_push(state) is None


def test_detect_precedent_push_returns_none_when_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")
    _git(["push", "origin", "loop/issue-1"], repo)
    head = _git(["rev-parse", "HEAD"], repo)
    state = lc._initial_state(
        "loop", "issue-loop", "hash", str(repo), "loop/issue-1", "implementation"
    )
    state.remote_head_baseline = head

    assert lc._detect_precedent_push(state) is None


def test_detect_precedent_push_returns_observed_head_on_drift(tmp_path: Path) -> None:
    """Simulates Issue #196: something (e.g. a Maker) pushed directly to `origin` -- the
    driver's own baseline (recorded before that push) is now stale."""
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")
    _git(["push", "origin", "loop/issue-1"], repo)
    stale_baseline = _git(["rev-parse", "HEAD"], repo)
    (repo / "extra.txt").write_text("out-of-band change\n", encoding="utf-8")
    _git(["add", "extra.txt"], repo)
    _git(["commit", "-m", "out-of-band push"], repo)
    _git(["push", "origin", "loop/issue-1"], repo)
    drifted_head = _git(["rev-parse", "HEAD"], repo)
    state = lc._initial_state(
        "loop", "issue-loop", "hash", str(repo), "loop/issue-1", "implementation"
    )
    state.remote_head_baseline = stale_baseline

    assert lc._detect_precedent_push(state) == drifted_head


def test_detect_precedent_push_returns_none_when_query_unverifiable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")
    _git(["push", "origin", "loop/issue-1"], repo)
    head = _git(["rev-parse", "HEAD"], repo)
    _git(["remote", "remove", "origin"], repo)
    state = lc._initial_state(
        "loop", "issue-loop", "hash", str(repo), "loop/issue-1", "implementation"
    )
    state.remote_head_baseline = head

    assert lc._detect_precedent_push(state) is None


def test_initial_state_seeds_remote_head_baseline_from_live_query(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")
    _git(["push", "origin", "loop/issue-1"], repo)
    expected = _git(["rev-parse", "HEAD"], repo)

    state = lc._initial_state(
        "loop",
        "issue-loop",
        "hash",
        str(repo),
        "loop/issue-1",
        "implementation",
        precedent_push_check=True,
    )

    assert state.remote_head_baseline == expected


def test_initial_state_default_does_not_query_remote_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`precedent_push_check` defaults to `False` (Issue #196 follow-up, PR review critical fix):
    LP-2's `loop_driver.py` (and any other non-opted-in caller of `start()`) must never pay the
    seed-on-creation `git ls-remote` network round trip, nor get a `remote_head_baseline` written
    to state -- even though the remote in this test has real commits that a query would find."""
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")
    _git(["push", "origin", "loop/issue-1"], repo)
    called = False

    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("_remote_head must not be queried when precedent_push_check=False")

    monkeypatch.setattr(lc, "_remote_head", _fail_if_called)

    state = lc._initial_state(
        "loop", "issue-loop", "hash", str(repo), "loop/issue-1", "implementation"
    )

    assert state.remote_head_baseline is None
    assert called is False


def _prepare_repo_for_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str = "loop/issue-1"
) -> str:
    """Real git repo + remote, plus the monkeypatches `lc.start()` needs.

    Unlike `_setup_real_git_loop()`, does not pre-create loop state -- `start()` creates it
    itself (and raises `InvalidStateError` if state already exists).
    """
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, branch)
    monkeypatch.setattr(lc, "resolve_root_worktree", lambda _project_dir: tmp_path)
    monkeypatch.setattr(lc.socket, "gethostname", lambda: "local")
    return str(repo)


def test_start_opt_out_omits_remote_head_baseline_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bot review finding (PR #277, Codex P2): non-opted-in `start()` callers (default
    `precedent_push_check=False`, e.g. LP-2's `loop_driver.py`) must get a persisted state.json
    and a `loop_created` journal payload with no `remote_head_baseline` key at all -- not a
    `null` value -- to stay byte-for-byte identical to the pre-Issue #196 shape."""
    branch = "loop/issue-1"
    project_dir = _prepare_repo_for_start(tmp_path, monkeypatch, branch)
    loop_id = "abcd1234-issue-1"

    lc.start(loop_id, project_dir, "issue-loop", "abcd1234", project_dir, branch, "owner", 3600)

    persisted = json.loads(lc.state_path(loop_id, project_dir).read_text(encoding="utf-8"))
    assert "remote_head_baseline" not in persisted

    journal_event = lc.find_journal_event(loop_id, project_dir, None, "loop_created")
    assert journal_event is not None
    assert "remote_head_baseline" not in journal_event["payload"]


def test_start_opt_in_includes_remote_head_baseline_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counterpart to the opt-out test above: opted-in callers (`loop_step.py`'s LP-1) must keep
    getting the seeded baseline in both state.json and the `loop_created` journal payload."""
    branch = "loop/issue-1"
    project_dir = _prepare_repo_for_start(tmp_path, monkeypatch, branch)
    repo = Path(project_dir)
    _git(["push", "origin", branch], repo)
    expected_head = _git(["rev-parse", "HEAD"], repo)
    loop_id = "abcd1234-issue-1"

    lc.start(
        loop_id,
        project_dir,
        "issue-loop",
        "abcd1234",
        project_dir,
        branch,
        "owner",
        3600,
        precedent_push_check=True,
    )

    persisted = json.loads(lc.state_path(loop_id, project_dir).read_text(encoding="utf-8"))
    assert persisted["remote_head_baseline"] == expected_head

    journal_event = lc.find_journal_event(loop_id, project_dir, None, "loop_created")
    assert journal_event is not None
    assert journal_event["payload"]["remote_head_baseline"] == expected_head


def test_initial_state_seeds_absent_sentinel_for_brand_new_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-9")  # never pushed

    state = lc._initial_state(
        "loop",
        "issue-loop",
        "hash",
        str(repo),
        "loop/issue-9",
        "implementation",
        precedent_push_check=True,
    )

    assert state.remote_head_baseline == lc.LP1_REMOTE_HEAD_ABSENT


def test_apply_advance_phase_refreshes_remote_head_baseline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")
    state = lc._initial_state(
        "loop",
        "issue-loop",
        "hash",
        str(repo),
        "loop/issue-1",
        "implementation",
        precedent_push_check=True,
    )
    assert state.remote_head_baseline == lc.LP1_REMOTE_HEAD_ABSENT
    # Orchestrator performs the legitimate advance_phase push before calling complete().
    _git(["push", "origin", "loop/issue-1"], repo)
    new_head = _git(["rev-parse", "HEAD"], repo)

    lc.apply_action_effect(state, lc.Action.ADVANCE_PHASE.value, {}, precedent_push_check=True)

    assert state.remote_head_baseline == new_head


def test_apply_advance_phase_keeps_baseline_when_precedent_push_check_disabled(
    tmp_path: Path,
) -> None:
    """`precedent_push_check` defaults to `False` (Issue #196): LP-2's `loop_driver.py` shares
    `apply_action_effect()` via `complete()` and already has its own push-integrity mechanism,
    so it must never pick up this refresh unless it explicitly opts in."""
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")
    state = lc._initial_state(
        "loop", "issue-loop", "hash", str(repo), "loop/issue-1", "implementation"
    )
    baseline_before = state.remote_head_baseline
    _git(["push", "origin", "loop/issue-1"], repo)

    lc.apply_action_effect(state, lc.Action.ADVANCE_PHASE.value, {})

    assert state.remote_head_baseline == baseline_before


def test_precompute_remote_head_refresh_disabled_returns_none(tmp_path: Path) -> None:
    """`_precompute_remote_head_refresh()` (Issue #196 follow-up, PR review high-severity fix)
    must not query the remote at all when `precedent_push_check` is `False`, regardless of
    `action`."""
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")
    _git(["push", "origin", "loop/issue-1"], repo)
    state = lc._initial_state(
        "loop", "issue-loop", "hash", str(repo), "loop/issue-1", "implementation"
    )

    result = lc._precompute_remote_head_refresh(
        state, lc.Action.ADVANCE_PHASE.value, None, None, precedent_push_check=False
    )

    assert result is None


def test_precompute_remote_head_refresh_advance_phase_returns_observed_head(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")
    _git(["push", "origin", "loop/issue-1"], repo)
    expected = _git(["rev-parse", "HEAD"], repo)
    state = lc._initial_state(
        "loop", "issue-loop", "hash", str(repo), "loop/issue-1", "implementation"
    )

    result = lc._precompute_remote_head_refresh(
        state, lc.Action.ADVANCE_PHASE.value, None, None, precedent_push_check=True
    )

    assert result == expected


def test_precompute_remote_head_refresh_wait_external_review_without_push_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `wait_external_review` completion that is not itself a push (last completed action was
    not `run_maker`) must not trigger the network query at all."""
    project_dir, lock = _setup_real_git_loop(tmp_path, monkeypatch, status="waiting_external")
    del lock
    state = lc.load_state("abcd1234-issue-1", project_dir)
    called = False

    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("_remote_head must not be queried without a required push")

    monkeypatch.setattr(lc, "_remote_head", _fail_if_called)

    result = lc._precompute_remote_head_refresh(
        state,
        lc.Action.WAIT_EXTERNAL_REVIEW.value,
        "abcd1234-issue-1",
        project_dir,
        precedent_push_check=True,
    )

    assert result is None
    assert called is False


def test_wait_external_review_required_push_refreshes_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #196: `wait_external_review` can itself carry a legitimate push (`push_required`,
    addressing PR review comments right after `run_maker`) -- the baseline must be refreshed
    here too, or that legitimate push would look like drift at the next `propose()` call."""
    project_dir, lock = _setup_real_git_loop(tmp_path, monkeypatch, status="waiting_external")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.phase = "pr_review_response"
    state.pr_number = 123
    assert state.remote_head_baseline == lc.LP1_REMOTE_HEAD_ABSENT
    lc.append_journal_event(
        "abcd1234-issue-1",
        project_dir,
        "completed",
        "maker",
        "act-maker-1",
        {"action": lc.Action.RUN_MAKER.value},
    )
    state.last_completed_action = lc.LastCompletedAction(
        action_id="act-maker-1",
        state_version_before=0,
        state_version_after=1,
        result_digest="x",
        completed_at=lc.now_iso(),
    )
    state.pending_action = lc.PendingAction(
        "act-wait", lc.Action.WAIT_EXTERNAL_REVIEW.value, "pr_review_response", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, project_dir)
    # Orchestrator addresses PR review comments and pushes before completing this action.
    _git(["push", "origin", "loop/issue-1"], Path(project_dir))
    new_head = _git(["rev-parse", "HEAD"], Path(project_dir))

    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        "act-wait",
        1,
        {"completed": False},
        lock.lease_token,
        precedent_push_check=True,
    )

    assert lc.load_state("abcd1234-issue-1", project_dir).remote_head_baseline == new_head


def test_wait_external_review_without_required_push_keeps_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The polling-only re-proposals of `wait_external_review` (last completed action is itself
    `wait_external_review`, not `run_maker`) never carry a new push, so the baseline must not be
    refreshed -- a real out-of-band push in between would otherwise be silently absorbed as if
    it were legitimate."""
    project_dir, lock = _setup_real_git_loop(tmp_path, monkeypatch, status="waiting_external")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.phase = "pr_review_response"
    state.pr_number = 123
    baseline_before = state.remote_head_baseline
    lc.append_journal_event(
        "abcd1234-issue-1",
        project_dir,
        "completed",
        "waiter",
        "act-wait-0",
        {"action": lc.Action.WAIT_EXTERNAL_REVIEW.value},
    )
    state.last_completed_action = lc.LastCompletedAction(
        action_id="act-wait-0",
        state_version_before=0,
        state_version_after=1,
        result_digest="x",
        completed_at=lc.now_iso(),
    )
    state.pending_action = lc.PendingAction(
        "act-wait-1", lc.Action.WAIT_EXTERNAL_REVIEW.value, "pr_review_response", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, project_dir)
    # No push happens on a pure polling re-proposal.

    lc.complete(
        "abcd1234-issue-1", project_dir, "act-wait-1", 1, {"completed": False}, lock.lease_token
    )

    assert lc.load_state("abcd1234-issue-1", project_dir).remote_head_baseline == baseline_before


def test_propose_warns_on_precedent_push_without_stopping_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates Issue #196: a Maker pushes directly to `origin` (bypassing the loop's own
    `advance_phase` push). The next `propose()` for `advance_phase` must record a
    `push_integrity_warning` journal event but must NOT stop the loop (LP-1 is warning-only;
    only a human-attended CLI, unlike LP-2's unattended fail-closed stop)."""
    project_dir, lock = _setup_real_git_loop(tmp_path, monkeypatch, status="running")
    repo = Path(project_dir)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.last_check_result = {"next_phase": "review"}
    state.state_version = 1
    lc._write_state(state, project_dir)
    assert state.remote_head_baseline == lc.LP1_REMOTE_HEAD_ABSENT
    # Out-of-band push: e.g. a Maker Task pushed directly, never going through advance_phase.
    _git(["push", "origin", "loop/issue-1"], repo)
    rogue_head = _git(["rev-parse", "HEAD"], repo)

    advance = lc.propose(
        "abcd1234-issue-1", project_dir, lock.lease_token, precedent_push_check=True
    )

    assert advance.action == lc.Action.ADVANCE_PHASE.value
    warning = lc.find_journal_event(
        "abcd1234-issue-1", project_dir, advance.action_id, "push_integrity_warning"
    )
    assert warning is not None
    assert warning["payload"]["observed_head"] == rogue_head
    assert warning["payload"]["expected_head"] == lc.LP1_REMOTE_HEAD_ABSENT
    assert advance.context["push_integrity_warning"] == {
        "expected_head": lc.LP1_REMOTE_HEAD_ABSENT,
        "observed_head": rogue_head,
    }
    assert lc.load_state("abcd1234-issue-1", project_dir).status != "stopped"


def test_propose_ignores_precedent_push_when_check_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`precedent_push_check` defaults to `False` (Issue #196): LP-2's `loop_driver.py` shares
    `propose()` and already has its own, separate push-integrity mechanism, so an LP-2-style
    caller that never passes this kwarg must see no warning and no extra `git ls-remote` call,
    even when a precedent push has in fact happened."""
    project_dir, lock = _setup_real_git_loop(tmp_path, monkeypatch, status="running")
    repo = Path(project_dir)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.last_check_result = {"next_phase": "review"}
    state.state_version = 1
    lc._write_state(state, project_dir)
    _git(["push", "origin", "loop/issue-1"], repo)

    advance = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)

    assert advance.action == lc.Action.ADVANCE_PHASE.value
    assert "push_integrity_warning" not in advance.context
    warning = lc.find_journal_event(
        "abcd1234-issue-1", project_dir, advance.action_id, "push_integrity_warning"
    )
    assert warning is None


def test_propose_does_not_warn_when_remote_head_matches_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir, lock = _setup_real_git_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.last_check_result = {"next_phase": "review"}
    state.state_version = 1
    lc._write_state(state, project_dir)

    advance = lc.propose(
        "abcd1234-issue-1", project_dir, lock.lease_token, precedent_push_check=True
    )

    assert advance.action == lc.Action.ADVANCE_PHASE.value
    warning = lc.find_journal_event(
        "abcd1234-issue-1", project_dir, advance.action_id, "push_integrity_warning"
    )
    assert warning is None


def test_complete_advance_phase_queries_remote_head_before_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR review high-severity fix (Issue #196 follow-up): `complete()`'s remote-head refresh
    query must run *before* its `guarded_lease_section` flock is acquired, not while the flock is
    held -- a slow/hanging `git ls-remote` must never block other flock-holding operations (lease
    renewal, heartbeat, `loop_status.py` purge, a concurrent `resume()`) for as long as that query
    takes."""
    project_dir, lock = _setup_real_git_loop(tmp_path, monkeypatch, status="running")
    repo = Path(project_dir)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        "act-advance", lc.Action.ADVANCE_PHASE.value, "implementation", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, project_dir)
    _git(["push", "origin", "loop/issue-1"], repo)
    new_head = _git(["rev-parse", "HEAD"], repo)

    events: list[str] = []
    real_remote_head = lc._remote_head
    real_guarded_lease_section = lc.guarded_lease_section

    def _tracking_remote_head(worktree_path: str, branch: str) -> str | None:
        events.append("remote_head")
        return real_remote_head(worktree_path, branch)

    @contextmanager
    def _tracking_guarded_lease_section(*args: str):
        events.append("lock_acquire")
        with real_guarded_lease_section(*args):
            yield
        events.append("lock_release")

    monkeypatch.setattr(lc, "_remote_head", _tracking_remote_head)
    monkeypatch.setattr(lc, "guarded_lease_section", _tracking_guarded_lease_section)

    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        "act-advance",
        1,
        {},
        lock.lease_token,
        precedent_push_check=True,
    )

    assert events == ["remote_head", "lock_acquire", "lock_release"]
    assert lc.load_state("abcd1234-issue-1", project_dir).remote_head_baseline == new_head


# --- Issue #196 PR review round 2 --------------------------------------------------------


def test_remote_head_returns_none_when_dangerous_git_config_present(tmp_path: Path) -> None:
    """ "Harden the remote-head probe against config rewrites": a noncompliant Maker that writes
    an `insteadOf`/`pushurl`-style entry into the shared worktree's local git config before the
    next `propose()`/`complete()` call must not be able to silently redirect this probe to an
    attacker-chosen remote while `remote.origin.url` itself still looks unchanged. `_remote_head()`
    must fail closed to `None` (its own existing "query unverifiable" outcome) the instant any
    dangerous local config key is present, even though the real branch really was pushed to the
    real `origin` underneath."""
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")
    _git(["push", "origin", "loop/issue-1"], repo)
    _git(
        ["config", "url.https://evil.example/repo.git.insteadOf", str(remote)],
        repo,
    )

    assert lc._remote_head(str(repo), "loop/issue-1") is None


def test_precompute_remote_head_refresh_exit_failure_with_draft_pr_push_returns_observed_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Refresh after failure-exit draft pushes": an `exit_failure` completion whose phase's
    `on_failure.exec` includes a Draft-PR push step must refresh the baseline exactly like
    `advance_phase`/`wait_external_review` already do."""
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")
    monkeypatch.setattr(
        lc,
        "_load_phase_definition",
        lambda _state, _project: {"on_failure": {"exec": ["pr_create_draft", "notify"]}},
    )
    _git(["push", "origin", "loop/issue-1"], repo)
    expected = _git(["rev-parse", "HEAD"], repo)
    state = lc._initial_state(
        "loop", "issue-loop", "hash", str(repo), "loop/issue-1", "implementation"
    )

    result = lc._precompute_remote_head_refresh(
        state, lc.Action.EXIT_FAILURE.value, None, str(repo), precedent_push_check=True
    )

    assert result == expected


def test_precompute_remote_head_refresh_exit_failure_without_push_step_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `exit_failure` completion whose `on_failure.exec` never pushes (e.g. `["notify"]` only)
    must not trigger the network query at all."""
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _init_repo_with_remote(repo, remote, "loop/issue-1")
    monkeypatch.setattr(
        lc, "_load_phase_definition", lambda _state, _project: {"on_failure": {"exec": ["notify"]}}
    )
    state = lc._initial_state(
        "loop", "issue-loop", "hash", str(repo), "loop/issue-1", "implementation"
    )

    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("_remote_head must not be queried without a push-capable exec step")

    monkeypatch.setattr(lc, "_remote_head", _fail_if_called)

    result = lc._precompute_remote_head_refresh(
        state, lc.Action.EXIT_FAILURE.value, None, str(repo), precedent_push_check=True
    )

    assert result is None


def test_complete_exit_failure_refreshes_baseline_so_resume_does_not_false_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end regression for the reviewer's exact scenario: an `exit_failure` completion
    that pushed a Draft PR branch must refresh `remote_head_baseline`, or `resume()`'d loop's
    very next `propose()` would misattribute that same push to the Maker as precedent-push drift
    (a false positive `push_integrity_warning` blaming the loop's own prior failure-exit push)."""
    project_dir, lock = _setup_real_git_loop(tmp_path, monkeypatch, status="failed")
    repo = Path(project_dir)
    monkeypatch.setattr(
        lc,
        "_load_phase_definition",
        lambda _state, _project: {"on_failure": {"exec": ["pr_create_draft", "notify"]}},
    )
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.stop_reason = "guard_failed"
    state.pending_action = lc.PendingAction(
        "act-exit-failure", lc.Action.EXIT_FAILURE.value, "implementation", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, project_dir)
    assert state.remote_head_baseline == lc.LP1_REMOTE_HEAD_ABSENT
    # Driver-owned failure-exit Draft PR push, landed before the CLI reports completion.
    _git(["push", "origin", "loop/issue-1"], repo)
    new_head = _git(["rev-parse", "HEAD"], repo)

    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        "act-exit-failure",
        1,
        {},
        lock.lease_token,
        precedent_push_check=True,
    )

    assert lc.load_state("abcd1234-issue-1", project_dir).remote_head_baseline == new_head
    resumed = lc.resume("abcd1234-issue-1", project_dir, True, "owner", 3600, host="local")

    advance = lc.propose(
        "abcd1234-issue-1", project_dir, resumed.lease_token, precedent_push_check=True
    )

    warning = lc.find_journal_event(
        "abcd1234-issue-1", project_dir, advance.action_id, "push_integrity_warning"
    )
    assert warning is None


def test_completed_payload_persists_remote_head_refresh_for_crash_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Persist remote head for crash replay": `complete()`'s `completed` journal event must
    include the precomputed remote head so `_reconcile_from_payload()` can restore
    `remote_head_baseline` after a crash between that journal write and `_write_state()`, instead
    of leaving the next proposal to compare against a stale/missing baseline."""
    project_dir, lock = _setup_real_git_loop(tmp_path, monkeypatch, status="running")
    repo = Path(project_dir)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        "act-advance", lc.Action.ADVANCE_PHASE.value, "implementation", 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, project_dir)
    _git(["push", "origin", "loop/issue-1"], repo)
    new_head = _git(["rev-parse", "HEAD"], repo)

    lc.complete(
        "abcd1234-issue-1",
        project_dir,
        "act-advance",
        1,
        {},
        lock.lease_token,
        precedent_push_check=True,
    )

    event = lc.find_journal_event("abcd1234-issue-1", project_dir, "act-advance", "completed")
    assert event is not None
    assert event["payload"]["remote_head_refresh"] == new_head


def test_reconcile_replays_persisted_remote_head_refresh_without_a_live_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates a crash between the `completed` journal write and `_write_state()`: the next
    `reconcile()` (e.g. via a fresh `attach()`) must restore `remote_head_baseline` from the
    journal payload alone -- not by re-querying `_remote_head()` live, which could observe a
    different value than the original completion did."""
    project_dir, lock = _setup_loop(tmp_path, monkeypatch, status="running")
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        "act-advance", lc.Action.ADVANCE_PHASE.value, "implementation", 1, lc.now_iso()
    )
    state.last_check_result = {"next_phase": "review"}
    state.state_version = 1
    lc._write_state(state, project_dir)
    persisted_head = "deadbeefcafef00d1234567890abcdef12345678"
    lc.append_journal_event(
        "abcd1234-issue-1",
        project_dir,
        "completed",
        "step",
        "act-advance",
        {
            "action": lc.Action.ADVANCE_PHASE.value,
            "result": {},
            "remote_head_refresh": persisted_head,
        },
    )

    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("reconcile must replay from the journal payload, not query live")

    monkeypatch.setattr(lc, "_remote_head", _fail_if_called)

    outcome = lc.reconcile("abcd1234-issue-1", project_dir, lock.lease_token)

    assert outcome.action_taken == "resolved_from_journal"
    assert lc.load_state("abcd1234-issue-1", project_dir).remote_head_baseline == persisted_head


# --- Issue #208 (SEC-H2): repo-identity re-verification hardening -----------------------------


def _linked_worktree_state(
    repo: Path,
    linked: Path,
    branch: str,
    *,
    repo_identity_hash: str | None = None,
    repo_identity_material_digest: str | None = None,
    worktree_gitlink_digest: str | None = None,
) -> lc.LoopState:
    """Build a `LoopState` pointing at `linked` (a real linked worktree of `repo`)."""
    state = lc._initial_state(
        "abcd1234-issue-1",
        "issue-loop",
        repo_identity_hash if repo_identity_hash is not None else lc._repo_identity_hash(str(repo)),
        str(linked),
        branch,
        "implementation",
    )
    state.repo_identity_material_digest = repo_identity_material_digest
    state.worktree_gitlink_digest = worktree_gitlink_digest
    return state


def test_is_repo_identity_verified_legacy_state_accepts_matching_truncated_hash(
    tmp_path: Path,
) -> None:
    """Pre-Issue-#208 state.json (no pinned digest/gitlink) keeps working unchanged."""
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    _init_repo(repo)
    _git(["worktree", "add", "-b", "loop/issue-1", str(linked), "HEAD"], repo)

    state = _linked_worktree_state(repo, linked, "loop/issue-1")

    assert lc.is_repo_identity_verified(state) is True


def test_is_repo_identity_verified_legacy_state_rejects_hash_mismatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    _init_repo(repo)
    _git(["worktree", "add", "-b", "loop/issue-1", str(linked), "HEAD"], repo)

    state = _linked_worktree_state(repo, linked, "loop/issue-1", repo_identity_hash="wrong0000")

    assert lc.is_repo_identity_verified(state) is False


def test_is_repo_identity_verified_prefers_pinned_material_digest_over_truncated_hash(
    tmp_path: Path,
) -> None:
    """A pinned `repo_identity_material_digest` is authoritative even if the (unused) legacy
    truncated hash on the same state happens to be stale."""
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    _init_repo(repo)
    _git(["worktree", "add", "-b", "loop/issue-1", str(linked), "HEAD"], repo)

    expected_digest = hashlib.sha256(
        lc._repo_identity_material(str(repo)).encode("utf-8")
    ).hexdigest()
    state = _linked_worktree_state(
        repo,
        linked,
        "loop/issue-1",
        repo_identity_hash="stale0000",
        repo_identity_material_digest=expected_digest,
    )

    assert lc.is_repo_identity_verified(state) is True


def test_is_repo_identity_verified_rejects_material_digest_mismatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    _init_repo(repo)
    _git(["worktree", "add", "-b", "loop/issue-1", str(linked), "HEAD"], repo)

    state = _linked_worktree_state(
        repo,
        linked,
        "loop/issue-1",
        repo_identity_material_digest="0" * 64,
    )

    assert lc.is_repo_identity_verified(state) is False


def test_is_repo_identity_verified_rejects_gitlink_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #208 (SEC-H2): rewriting the linked worktree's `.git` gitlink pointer -- even
    while leaving it syntactically valid and still resolving to the same real gitdir -- must
    flip verification to `False` once a baseline was pinned."""
    from tests.module_loader import load_module

    wm = load_module("worktree_manager", "packages/loop-harness/lib/worktree_manager.py")
    monkeypatch.setattr(lc.socket, "gethostname", lambda: "local")

    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    _init_repo(repo)
    _git(["worktree", "add", "-b", "loop/issue-1", str(linked), "HEAD"], repo)
    pinned_gitlink = wm.gitlink_fingerprint(str(linked))
    assert pinned_gitlink is not None

    state = _linked_worktree_state(
        repo,
        linked,
        "loop/issue-1",
        # Both digest fields must be set together, matching how `create_worktree()` + `start()`
        # always populate them for a real (post-Issue-#208) loop -- `is_repo_identity_verified()`
        # gates its whole hardened path behind `repo_identity_material_digest is not None`.
        repo_identity_material_digest=wm.resolve_repo_identity_material_digest(str(repo)),
        worktree_gitlink_digest=pinned_gitlink,
    )
    assert lc.is_repo_identity_verified(state) is True

    # `/.` is a harmless self-referencing path suffix (still resolves to the identical
    # directory on POSIX), so this changes the gitlink file's byte content -- and therefore its
    # fingerprint -- without breaking the worktree's own git functionality (confirmed: `git -C
    # <worktree> rev-parse --git-common-dir`/`config --get` both still resolve correctly).
    gitlink_path = linked / ".git"
    original = gitlink_path.read_text(encoding="utf-8")
    gitlink_path.write_text(original.rstrip("\n") + "/.\n", encoding="utf-8")

    assert lc.is_repo_identity_verified(state) is False


def test_is_repo_identity_verified_ignores_gitlink_drift_when_digest_not_pinned(
    tmp_path: Path,
) -> None:
    """Back-compat: a loop that predates Issue #208 (`worktree_gitlink_digest is None`) has no
    baseline to compare against, so gitlink drift is not (and cannot be) newly flagged."""
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    _init_repo(repo)
    _git(["worktree", "add", "-b", "loop/issue-1", str(linked), "HEAD"], repo)

    state = _linked_worktree_state(repo, linked, "loop/issue-1")

    # `/.` is a harmless self-referencing path suffix (still resolves to the identical
    # directory on POSIX), so this changes the gitlink file's byte content -- and therefore its
    # fingerprint -- without breaking the worktree's own git functionality (confirmed: `git -C
    # <worktree> rev-parse --git-common-dir`/`config --get` both still resolve correctly).
    gitlink_path = linked / ".git"
    original = gitlink_path.read_text(encoding="utf-8")
    gitlink_path.write_text(original.rstrip("\n") + "/.\n", encoding="utf-8")

    assert lc.is_repo_identity_verified(state) is True


def test_is_repo_identity_verified_rejects_dangerous_local_git_config(tmp_path: Path) -> None:
    """Issue #208 (SEC-H2): a Maker `Edit`-write of an `insteadOf` rewrite into the (shared)
    worktree's local git config must fail identity re-verification, matching the same
    dangerous-key family `loop_driver_support.find_dangerous_local_git_config()` already
    screens for before every driver-owned push."""
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    _init_repo(repo)
    _git(["worktree", "add", "-b", "loop/issue-1", str(linked), "HEAD"], repo)
    origin_url = str(repo.resolve())
    _git(["remote", "add", "origin", origin_url], repo)
    material_digest = hashlib.sha256(
        lc._repo_identity_material(str(repo)).encode("utf-8")
    ).hexdigest()

    state = _linked_worktree_state(
        repo, linked, "loop/issue-1", repo_identity_material_digest=material_digest
    )
    assert lc.is_repo_identity_verified(state) is True

    _git(["config", "url.https://evil.example/.insteadOf", origin_url], linked)

    assert lc.is_repo_identity_verified(state) is False


def test_is_repo_identity_verified_rejects_when_git_config_scan_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #208 (SEC-H2) review finding (high): `find_dangerous_local_git_config()`'s
    fail-open `None` return conflates "scanned and clean" with "scan itself could not be
    completed" (process error/timeout). A Maker that makes the scan fail (e.g. by making
    `.git/config` transiently unreadable right before a driver re-verification call) must not
    be treated as clean by identity re-verification -- an unscannable config must fail closed
    here, unlike the intentionally fail-open push-guard callers of the same underlying scan."""
    lds = load_module("loop_driver_support", "packages/loop-harness/lib/loop_driver_support.py")

    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    _init_repo(repo)
    _git(["worktree", "add", "-b", "loop/issue-1", str(linked), "HEAD"], repo)
    material_digest = hashlib.sha256(
        lc._repo_identity_material(str(repo)).encode("utf-8")
    ).hexdigest()

    state = _linked_worktree_state(
        repo, linked, "loop/issue-1", repo_identity_material_digest=material_digest
    )
    assert lc.is_repo_identity_verified(state) is True

    monkeypatch.setattr(
        lds, "local_git_config_scan_result", lambda _cwd, _timeout_seconds=10.0: (False, None)
    )

    assert lc.is_repo_identity_verified(state) is False
