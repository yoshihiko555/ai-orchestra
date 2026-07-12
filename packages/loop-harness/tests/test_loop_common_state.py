"""State machine, journal, and two-phase tests for loop_common."""

from __future__ import annotations

import json
import os
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
