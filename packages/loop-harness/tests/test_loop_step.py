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
        "abcd1234",
        str(repo / ".worktrees" / "loop-issue-1"),
        "loop/issue-1",
        "implementation",
    )
    state.status = "running"
    lc._write_state(state, str(repo))
    lock = lc.acquire_lock(loop_id, str(repo), "owner", 3600, host=socket.gethostname())
    assert lock is not None
    return lock


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

    check_result = {
        "passed": True,
        "signature": "",
        "infrastructure_failure": False,
        "results": [],
    }
    phase_def = {
        "on_success": {"disposition": "advance_phase", "next": "pr_review_response"},
        "on_failure": {"disposition": "exit_failure"},
    }
    _complete(
        repo,
        started["loop_id"],
        checker,
        started["lease_token"],
        {"check_result": check_result, "phase_def": phase_def},
    )

    advance = _propose(repo, started["loop_id"], started["lease_token"])

    assert advance["action"] == lc.Action.ADVANCE_PHASE.value
    assert advance["params"] == {
        "verified_branch": "loop/issue-7",
        "next_phase": "pr_review_response",
        "exec": ["commit", "push", "pr_create"],
    }


def test_complete_checker_persists_check_result_artifact(tmp_path: Path) -> None:
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

    _complete(
        repo,
        started["loop_id"],
        checker,
        started["lease_token"],
        {"check_result": check_result},
    )

    artifact = lc.load_artifact(
        started["loop_id"], str(repo), checker["action_id"], "check_result.json"
    )
    assert artifact is not None
    assert json.loads(artifact) == check_result


def test_complete_checker_persists_raw_check_result_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    started = _start(repo)
    _complete(repo, started["loop_id"], started, started["lease_token"])
    checker = _propose(repo, started["loop_id"], started["lease_token"])
    check_result = {
        "passed": False,
        "signature": "sig-raw",
        "infrastructure_failure": False,
        "results": [],
    }

    _complete(repo, started["loop_id"], checker, started["lease_token"], check_result)

    artifact = lc.load_artifact(
        started["loop_id"], str(repo), checker["action_id"], "check_result.json"
    )
    assert artifact is not None
    assert json.loads(artifact) == check_result


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
    lc._write_state(state, str(repo))

    payload = _propose(repo, "abcd1234-issue-1", lock.lease_token)

    assert payload["action"] == lc.Action.EXIT_FAILURE.value
    assert payload["params"] == {
        "stop_reason": "max_iterations",
        "draft_pr_exec": ["pr_create_draft", "notify"],
    }


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


def test_foreign_live_lease_safety_stop_is_surfaced_by_propose(tmp_path: Path) -> None:
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

    assert proc.returncode == 0, proc.stderr
    assert payload["action"] == lc.Action.STOP.value
    assert payload["params"]["stop_reason"] == "foreign_live_lease"
    assert lc.load_state(loop_id, str(repo)).status == "stopped"


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
