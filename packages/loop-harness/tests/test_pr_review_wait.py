"""PR review wait/import tests for loop-harness."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.module_loader import load_module

prw = load_module("pr_review_wait_tests", "packages/loop-harness/lib/pr_review_wait.py")
lc = prw.lc


class FakeClient:
    """Route-based fake GitHub API client."""

    repo = "owner/repo"

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def api(self, path: str) -> Any:
        self.calls.append(path)
        value = self.routes.get(path, [])
        if isinstance(value, Exception):
            raise value
        return value


def _setup_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, pr_review: dict[str, Any] | None = None
) -> str:
    monkeypatch.setattr(lc, "resolve_root_worktree", lambda _project_dir: tmp_path)
    monkeypatch.setattr(lc, "_repo_identity_hash", lambda _project_dir: "hash")
    monkeypatch.setattr(lc, "_current_branch", lambda _project_dir: "loop/issue-1")
    state = lc._initial_state(
        "abcd1234-issue-1",
        "issue-loop",
        "hash",
        str(tmp_path),
        "loop/issue-1",
        "pr_review_response",
    )
    state.pr_number = 12
    state.pr_review = pr_review
    state.status = "running"
    lc._write_state(state, str(tmp_path))
    return str(tmp_path)


def _lease(project_dir: str) -> str:
    lock = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 3600)
    assert lock is not None
    return lock.lease_token


def _config(
    *,
    checkruns: tuple[str, ...] = (),
    auto_generated_markers: tuple[str, ...] = prw.DEFAULT_AUTO_GENERATED_MARKERS,
) -> prw.PrReviewConfig:
    return prw.PrReviewConfig(
        reviewer_allowlist=(
            prw.ReviewerAllowlistEntry(
                app_slug="codex-app",
                login="codex[bot]",
                user_type="Bot",
                author_associations=frozenset({"NONE"}),
            ),
        ),
        checkrun_allowlist=frozenset(checkruns),
        poll_interval_seconds=1,
        timeout_seconds=0,
        auto_generated_markers=auto_generated_markers,
    )


def _mock_git_head(
    monkeypatch: pytest.MonkeyPatch, sha: str | None, *, returncode: int = 0
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        stdout = f"{sha}\n" if sha is not None else ""
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(prw.subprocess, "run", fake_run)
    return calls


def _trusted(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {
        "user": {"login": "codex[bot]", "type": "Bot"},
        "author_association": "NONE",
        "performed_via_github_app": {"slug": "codex-app"},
    }
    return {**data, **(extra or {})}


def _untrusted(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {
        "user": {"login": "random-user", "type": "User"},
        "author_association": "NONE",
    }
    return {**data, **(extra or {})}


def test_ev30_records_baseline_before_iteration_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [{"id": 7}, {"id": 10}],
            "repos/owner/repo/pulls/12/comments": [{"id": 100}],
            "repos/owner/repo/issues/12/comments": [{"id": 200}],
            "repos/owner/repo/pulls/12": {"head": {"sha": "abc123"}},
        }
    )

    baseline = prw.record_baseline("abcd1234-issue-1", project_dir, 12, client, lease_token)
    sha = prw.record_iteration_head("abcd1234-issue-1", project_dir, 12, client, lease_token)

    state = lc.load_state("abcd1234-issue-1", project_dir)
    assert baseline.baseline_review_id == 10
    assert sha == "abc123"
    assert state.pr_review["baseline_review_id"] == 10
    assert state.pr_review["iteration_head_sha"] == "abc123"
    assert state.state_version == 2
    assert client.calls == [
        "repos/owner/repo/pulls/12/reviews",
        "repos/owner/repo/pulls/12/comments",
        "repos/owner/repo/issues/12/comments",
        "repos/owner/repo/pulls/12",
    ]


def test_ev76_detects_no_new_commit_when_local_head_matches_iteration_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "abc123"
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={"iteration_head_sha": sha, "processed_comment_ids": [], "findings": {}},
    )
    calls = _mock_git_head(monkeypatch, sha)

    delta = prw.detect_pr_review_push_delta("abcd1234-issue-1", project_dir, project_dir)

    assert delta.status == "no_new_commit"
    assert delta.local_head_sha == sha
    assert delta.iteration_head_sha == sha
    assert calls == [["git", "-C", project_dir, "rev-parse", "HEAD"]]


def test_ev76_detects_new_commit_when_local_head_differs_from_iteration_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={"iteration_head_sha": "old123", "processed_comment_ids": [], "findings": {}},
    )
    _mock_git_head(monkeypatch, "new456")

    delta = prw.detect_pr_review_push_delta("abcd1234-issue-1", project_dir, project_dir)

    assert delta.status == "new_commit"
    assert delta.local_head_sha == "new456"
    assert delta.iteration_head_sha == "old123"


def test_ev76_detects_unknown_when_iteration_head_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={"processed_comment_ids": [], "findings": {}},
    )
    _mock_git_head(monkeypatch, "abc123")

    delta = prw.detect_pr_review_push_delta("abcd1234-issue-1", project_dir, project_dir)

    assert delta.status == "unknown"
    assert delta.local_head_sha == "abc123"
    assert delta.iteration_head_sha is None


def test_ev76_detects_unknown_when_git_head_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={"iteration_head_sha": "abc123", "processed_comment_ids": [], "findings": {}},
    )
    _mock_git_head(monkeypatch, None, returncode=1)

    delta = prw.detect_pr_review_push_delta("abcd1234-issue-1", project_dir, project_dir)

    assert delta.status == "unknown"
    assert delta.local_head_sha is None
    assert delta.iteration_head_sha == "abc123"


def test_ev76_no_new_commit_outcome_is_timeout_shaped_with_shortcut_metadata() -> None:
    delta = prw.PrReviewPushDelta("no_new_commit", "abc123", "abc123")

    outcome = prw.no_new_commit_completion_outcome(delta)
    phase_check = prw.phase_check_from_completion_outcome(outcome)

    assert outcome.signal == "timeout"
    assert outcome.completed is False
    assert outcome.timed_out is True
    assert outcome.infrastructure_failure is False
    assert outcome.shortcut_reason == "no_new_commit_to_push"
    assert outcome.local_head_sha == "abc123"
    assert outcome.iteration_head_sha == "abc123"
    assert phase_check.passed is False
    assert phase_check.signature == "pr_review_timeout"
    assert phase_check.infrastructure_failure is False
    assert phase_check.metadata["shortcut_reason"] == "no_new_commit_to_push"
    assert phase_check.metadata["local_head_sha"] == "abc123"
    assert phase_check.metadata["iteration_head_sha"] == "abc123"


@pytest.mark.parametrize("status", ["new_commit", "unknown"])
def test_ev76_no_new_commit_outcome_rejects_non_shortcut_status(status: str) -> None:
    delta = prw.PrReviewPushDelta(status, "local", "recorded")

    with pytest.raises(prw.PrReviewWaitError, match="status=no_new_commit"):
        prw.no_new_commit_completion_outcome(delta)


def test_pr_review_auxiliary_updates_preserve_pending_action_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    proposal = lc.propose("abcd1234-issue-1", project_dir, lease_token)
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [],
            "repos/owner/repo/pulls/12": {"head": {"sha": "abc123"}},
        }
    )

    prw.record_baseline(
        "abcd1234-issue-1",
        project_dir,
        12,
        client,
        lease_token,
        action_id=proposal.action_id,
    )
    prw.record_iteration_head(
        "abcd1234-issue-1",
        project_dir,
        12,
        client,
        lease_token,
        action_id=proposal.action_id,
    )
    client.routes["repos/owner/repo/issues/12/comments"] = [
        _trusted(
            {
                "id": 200,
                "created_at": "2099-01-01T00:00:00+00:00",
                "body": "Please consider this edge case",
            }
        )
    ]
    collected = prw.collect_review_findings(
        "abcd1234-issue-1",
        project_dir,
        12,
        _config(),
        client,
        proposal.iteration,
        lease_token,
        action_id=proposal.action_id,
    )
    prw.apply_severity_classifications(
        "abcd1234-issue-1",
        project_dir,
        collected,
        _config(),
        {"issue_comment:200": "SEVERITY: medium\nCONFIDENCE: high\n"},
        proposal.iteration,
        lease_token,
        action_id=proposal.action_id,
    )

    state_before_complete = lc.load_state("abcd1234-issue-1", project_dir)
    completed = lc.complete(
        "abcd1234-issue-1",
        project_dir,
        proposal.action_id,
        proposal.state_version,
        {"completed": False},
        lease_token,
    )

    assert proposal.action == lc.Action.WAIT_EXTERNAL_REVIEW.value
    assert state_before_complete.state_version == proposal.state_version
    assert completed.ok is True


def test_bound_baseline_rejects_action_replaced_during_external_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        "act-old", lc.Action.WAIT_EXTERNAL_REVIEW.value, state.phase, 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, project_dir)

    class ReplacingClient(FakeClient):
        replaced = False

        def api(self, path: str) -> Any:
            if not self.replaced:
                current = lc.load_state("abcd1234-issue-1", project_dir)
                current.pending_action = lc.PendingAction(
                    "act-new",
                    lc.Action.WAIT_EXTERNAL_REVIEW.value,
                    current.phase,
                    1,
                    lc.now_iso(),
                )
                current.state_version += 1
                lc._write_state(current, project_dir)
                self.replaced = True
            return super().api(path)

    client = ReplacingClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [],
        }
    )

    with pytest.raises(lc.StaleActionError):
        prw.record_baseline(
            "abcd1234-issue-1",
            project_dir,
            12,
            client,
            lease_token,
            action_id="act-old",
        )

    current = lc.load_state("abcd1234-issue-1", project_dir)
    assert current.pr_review is None
    journal = lc.journal_path("abcd1234-issue-1", project_dir)
    assert not journal.exists()


def test_bound_collect_rejects_action_replaced_during_external_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "baseline_review_id": 0,
            "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
            "processed_comment_ids": [],
            "findings": {},
        },
    )
    lease_token = _lease(project_dir)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        "act-old", lc.Action.WAIT_EXTERNAL_REVIEW.value, state.phase, 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, project_dir)

    class ReplacingClient(FakeClient):
        replaced = False

        def api(self, path: str) -> Any:
            if not self.replaced:
                current = lc.load_state("abcd1234-issue-1", project_dir)
                current.pending_action = lc.PendingAction(
                    "act-new",
                    lc.Action.WAIT_EXTERNAL_REVIEW.value,
                    current.phase,
                    1,
                    lc.now_iso(),
                )
                current.state_version += 1
                lc._write_state(current, project_dir)
                self.replaced = True
            return super().api(path)

    client = ReplacingClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [],
        }
    )

    with pytest.raises(lc.StaleActionError):
        prw.collect_review_findings(
            "abcd1234-issue-1",
            project_dir,
            12,
            _config(),
            client,
            1,
            lease_token,
            action_id="act-old",
        )

    current = lc.load_state("abcd1234-issue-1", project_dir)
    assert current.pr_review["processed_comment_ids"] == []
    journal = lc.journal_path("abcd1234-issue-1", project_dir)
    assert not journal.exists()


def test_bound_pr_review_update_rejects_disallowed_pending_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        "act-maker", lc.Action.RUN_MAKER.value, state.phase, 1, lc.now_iso()
    )
    state.state_version = 1
    lc._write_state(state, project_dir)

    with pytest.raises(lc.ProtocolViolationError):
        prw.record_baseline(
            "abcd1234-issue-1",
            project_dir,
            None,
            FakeClient({}),
            lease_token,
            action_id="act-maker",
        )

    assert lc.load_state("abcd1234-issue-1", project_dir).state_version == 1


def test_ev30_records_empty_baseline_when_pr_does_not_exist_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    client = FakeClient({})

    baseline = prw.record_baseline("abcd1234-issue-1", project_dir, None, client, lease_token)

    state = lc.load_state("abcd1234-issue-1", project_dir)
    assert baseline.baseline_review_id == 0
    assert baseline.processed_comment_ids == ()
    assert state.pr_review["baseline_review_id"] == 0
    assert state.pr_review["processed_comment_ids"] == []
    assert client.calls == []


def test_ev31_checkrun_fallback_is_disabled_when_allowlist_empty() -> None:
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/commits/abc/check-runs": {
                "check_runs": [{"name": "Codex Review", "status": "completed"}]
            },
        }
    )

    outcome = prw.wait_for_completion(
        12,
        {"baseline_review_id": 10, "iteration_head_sha": "abc"},
        _config(),
        client,
        sleeper=lambda _seconds: None,
    )

    assert outcome.signal == "timeout"
    assert outcome.timed_out is True
    assert client.calls == ["repos/owner/repo/pulls/12/reviews"]


def test_ev31_checkrun_fallback_requires_configured_allowlist() -> None:
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/commits/abc/check-runs": {
                "check_runs": [{"name": "Codex Review", "status": "completed"}]
            },
        }
    )

    outcome = prw.wait_for_completion(
        12,
        {"baseline_review_id": 10, "iteration_head_sha": "abc"},
        _config(checkruns=("Codex Review",)),
        client,
        sleeper=lambda _seconds: None,
    )

    assert outcome.signal == "check_run_completed"
    assert outcome.completed is True
    assert outcome.check_run_names == ("Codex Review",)


def test_checkrun_completion_preserves_observed_untrusted_reviews() -> None:
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [
                _untrusted(
                    {
                        "id": 11,
                        "state": "COMMENTED",
                        "submitted_at": "2026-07-09T00:00:01+00:00",
                        "body": "untrusted review",
                    }
                )
            ],
            "repos/owner/repo/commits/abc/check-runs": {
                "check_runs": [{"name": "Codex Review", "status": "completed"}]
            },
        }
    )

    outcome = prw.wait_for_completion(
        12,
        {"baseline_review_id": 10, "iteration_head_sha": "abc"},
        _config(checkruns=("Codex Review",)),
        client,
        sleeper=lambda _seconds: None,
    )

    assert outcome.signal == "check_run_completed"
    assert outcome.ignored_untrusted_review_count == 1
    assert outcome.ignored_untrusted_reviews[0].review_id == 11


def test_wait_for_completion_ignores_unsubmitted_draft_reviews() -> None:
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [
                _trusted({"id": 11, "state": "PENDING", "submitted_at": None, "body": "[P1] draft"})
            ]
        }
    )

    outcome = prw.wait_for_completion(
        12,
        {"baseline_review_id": 10},
        _config(),
        client,
        sleeper=lambda _seconds: None,
    )

    assert outcome.signal == "timeout"
    assert outcome.completed is False
    assert outcome.ignored_untrusted_review_count == 0


def test_wait_for_completion_returns_and_records_untrusted_submitted_reviews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "baseline_review_id": 10,
            "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
            "processed_comment_ids": [],
            "findings": {},
        },
    )
    lease_token = _lease(project_dir)
    untrusted_review = _untrusted(
        {
            "id": 11,
            "state": "COMMENTED",
            "submitted_at": "2026-07-09T00:00:01+00:00",
            "body": "[P1] untrusted submitted review",
        }
    )
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [untrusted_review],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [],
        }
    )

    outcome = prw.wait_for_completion(
        12,
        {"baseline_review_id": 10},
        _config(),
        client,
        sleeper=lambda _seconds: None,
    )
    recorded = prw.record_ignored_untrusted_reviews(
        "abcd1234-issue-1", project_dir, outcome, lease_token
    )
    recorded_again = prw.record_ignored_untrusted_reviews(
        "abcd1234-issue-1", project_dir, outcome, lease_token
    )
    collect_result = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, _config(), client, 1, lease_token
    )
    state = lc.load_state("abcd1234-issue-1", project_dir)
    journal = Path(project_dir) / ".claude" / "loop" / "abcd1234-issue-1" / "journal.jsonl"
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]

    assert outcome.signal == "timeout"
    assert outcome.ignored_untrusted_review_count == 1
    assert outcome.ignored_untrusted_reviews[0].review_id == 11
    assert recorded == 1
    assert recorded_again == 0
    assert collect_result.ignored_untrusted_comment_count == 0
    assert state.ignored_untrusted_comment_count == 1
    assert "review:11" in state.pr_review["processed_comment_ids"]
    ignored_events = [event for event in events if event["event"] == "ignored_untrusted_comment"]
    assert len(ignored_events) == 1
    assert ignored_events[0]["payload"]["source"] == "review"
    assert ignored_events[0]["payload"]["comment_id"] == "11"
    assert ignored_events[0]["payload"]["notification_required"] is True


def test_ev32_timeout_is_not_infrastructure_failure_but_api_error_is() -> None:
    timeout = prw.phase_check_from_completion_outcome(
        prw.CompletionOutcome("timeout", False, True, False)
    )
    api_error = prw.phase_check_from_completion_outcome(
        prw.CompletionOutcome("api_error", False, False, True, error="rate limit")
    )

    assert timeout.infrastructure_failure is False
    assert timeout.signature == "pr_review_timeout"
    assert "shortcut_reason" not in timeout.metadata
    assert "local_head_sha" not in timeout.metadata
    assert "iteration_head_sha" not in timeout.metadata
    assert api_error.infrastructure_failure is True
    assert api_error.signature == "pr_review_api_error"


def test_ev32_api_call_failure_returns_infrastructure_outcome() -> None:
    client = FakeClient({"repos/owner/repo/pulls/12/reviews": prw.GitHubApiError("rate limit")})

    outcome = prw.wait_for_completion(
        12,
        {"baseline_review_id": 10},
        _config(),
        client,
        sleeper=lambda _seconds: None,
    )

    assert outcome.signal == "api_error"
    assert outcome.infrastructure_failure is True
    assert outcome.timed_out is False


def test_wait_for_completion_calls_heartbeat_during_poll_wait() -> None:
    client = FakeClient({"repos/owner/repo/pulls/12/reviews": []})
    times = iter([0.0, 0.0, 2.0])
    heartbeats: list[str] = []
    config = prw.PrReviewConfig(
        reviewer_allowlist=_config().reviewer_allowlist,
        poll_interval_seconds=1,
        timeout_seconds=1,
    )

    outcome = prw.wait_for_completion(
        12,
        {"baseline_review_id": 10},
        config,
        client,
        monotonic=lambda: next(times),
        sleeper=lambda _seconds: None,
        heartbeat=lambda: heartbeats.append("beat"),
    )

    assert outcome.signal == "timeout"
    assert heartbeats == ["beat"]


def test_state_writes_reject_invalid_lease_before_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [{"id": 1}],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [],
        }
    )

    with pytest.raises(lc.WriteRejectedError):
        prw.record_baseline("abcd1234-issue-1", project_dir, 12, client, "bad-lease")

    assert not (
        Path(project_dir) / ".claude" / "loop" / "abcd1234-issue-1" / "journal.jsonl"
    ).exists()


def test_ev33_ev34_ev35_collects_only_trusted_post_baseline_unprocessed_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "baseline_review_id": 10,
            "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
            "processed_comment_ids": ["review_comment:2"],
            "findings": {},
        },
    )
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/pulls/12/comments": [
                _trusted(
                    {
                        "id": 1,
                        "created_at": "2026-07-08T23:59:59+00:00",
                        "pull_request_review_id": 11,
                        "body": "[P1] old",
                        "path": "app.py",
                        "line": 10,
                    }
                ),
                _trusted(
                    {
                        "id": 2,
                        "created_at": "2026-07-09T00:00:01+00:00",
                        "pull_request_review_id": 11,
                        "body": "[P1] already processed",
                        "path": "app.py",
                        "line": 10,
                    }
                ),
                _trusted(
                    {
                        "id": 3,
                        "created_at": "2026-07-09T00:00:02+00:00",
                        "pull_request_review_id": 11,
                        "body": "[P2] Please add null check",
                        "path": "./app.py",
                        "line": None,
                        "original_line": 17,
                    }
                ),
                _untrusted(
                    {
                        "id": 4,
                        "created_at": "2026-07-09T00:00:03+00:00",
                        "pull_request_review_id": 11,
                        "body": "[P1] untrusted",
                        "path": "app.py",
                        "line": 20,
                    }
                ),
            ],
            "repos/owner/repo/issues/12/comments": [
                _trusted(
                    {
                        "id": 3,
                        "created_at": "2026-07-09T00:00:04+00:00",
                        "body": "[P4] general note",
                    }
                )
            ],
        }
    )

    result = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, _config(), client, 1, _lease(project_dir)
    )
    state = lc.load_state("abcd1234-issue-1", project_dir)
    journal = Path(project_dir) / ".claude" / "loop" / "abcd1234-issue-1" / "journal.jsonl"
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]

    assert [item.source_comment_id for item in result.findings] == [
        "review_comment:3",
        "issue_comment:3",
    ]
    assert "review_comment:3" in result.processed_comment_ids
    assert "issue_comment:3" in result.processed_comment_ids
    assert result.ignored_untrusted_comment_count == 1
    assert state.ignored_untrusted_comment_count == 1
    ignored_events = [event for event in events if event["event"] == "ignored_untrusted_comment"]
    assert ignored_events[0]["payload"]["comment_id"] == "4"
    assert ignored_events[0]["payload"]["notification_required"] is True


def test_collect_review_findings_accepts_same_second_post_baseline_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "baseline_review_id": 10,
            "baseline_recorded_at": "2026-07-09T00:00:00.900000+00:00",
            "processed_comment_ids": [],
            "findings": {},
        },
    )
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/pulls/12/comments": [
                _trusted(
                    {
                        "id": 5,
                        "created_at": "2026-07-09T00:00:00+00:00",
                        "pull_request_review_id": 11,
                        "body": "[P2] Same-second review comment",
                        "path": "app.py",
                        "line": 10,
                    }
                )
            ],
            "repos/owner/repo/issues/12/comments": [],
        }
    )

    result = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, _config(), client, 1, _lease(project_dir)
    )

    assert [item.source_comment_id for item in result.findings] == ["review_comment:5"]


def test_collect_review_findings_skips_positive_review_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "baseline_review_id": 10,
            "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
            "processed_comment_ids": [],
            "findings": {},
        },
    )
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [
                _trusted(
                    {
                        "id": 11,
                        "state": "APPROVED",
                        "submitted_at": "2026-07-09T00:00:01+00:00",
                        "body": "LGTM",
                    }
                )
            ],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [],
        }
    )

    result = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, _config(), client, 1, _lease(project_dir)
    )

    assert result.findings == ()
    assert "review:11" in result.processed_comment_ids


def test_collect_review_findings_skips_positive_issue_comment_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "baseline_review_id": 10,
            "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
            "processed_comment_ids": [],
            "findings": {},
        },
    )
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [
                _trusted(
                    {
                        "id": 12,
                        "created_at": "2026-07-09T00:00:01+00:00",
                        "body": "No issues found",
                    }
                )
            ],
        }
    )

    result = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, _config(), client, 1, _lease(project_dir)
    )

    assert result.findings == ()
    assert "issue_comment:12" in result.processed_comment_ids


_CODERABBIT_MARKER = "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->"


def test_collect_review_findings_skips_auto_generated_bot_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #183: a bot summary comment containing 'High' must not become a phantom finding."""
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "baseline_review_id": 10,
            "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
            "processed_comment_ids": [],
            "findings": {},
        },
    )
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [
                _trusted(
                    {
                        "id": 13,
                        "created_at": "2026-07-09T00:00:01+00:00",
                        "body": f"{_CODERABBIT_MARKER}\n## Summary\n\nSeverity: High\n\nWalkthrough...",
                    }
                )
            ],
        }
    )

    result = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, _config(), client, 1, _lease(project_dir)
    )

    assert result.findings == ()
    assert "issue_comment:13" in result.processed_comment_ids


def test_collect_review_findings_imports_actionable_coderabbit_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #183 / PR #188: actionable CodeRabbit comments must remain real findings."""
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "baseline_review_id": 10,
            "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
            "processed_comment_ids": [],
            "findings": {},
        },
    )
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [
                _trusted(
                    {
                        "id": 16,
                        "created_at": "2026-07-09T00:00:01+00:00",
                        "body": "<!-- This is an auto-generated comment by CodeRabbit -->\n"
                        "**Actionable comments posted: 2**\n\n[HIGH] Something is broken here",
                    }
                )
            ],
        }
    )

    result = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, _config(), client, 1, _lease(project_dir)
    )

    assert result.findings
    assert result.findings[0].severity == "high"


def test_parse_pr_review_config_defaults_auto_generated_markers() -> None:
    config = prw.parse_pr_review_config(
        {"pr_review": {"reviewer_allowlist": [{"app_slug": "codex-app"}]}}
    )
    assert config.auto_generated_markers == prw.DEFAULT_AUTO_GENERATED_MARKERS


def test_parse_pr_review_config_empty_list_disables_auto_generated_filter() -> None:
    config = prw.parse_pr_review_config(
        {
            "pr_review": {
                "reviewer_allowlist": [{"app_slug": "codex-app"}],
                "auto_generated_markers": [],
            }
        }
    )
    assert config.auto_generated_markers == ()


def test_collect_review_findings_imports_when_auto_generated_filter_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "baseline_review_id": 10,
            "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
            "processed_comment_ids": [],
            "findings": {},
        },
    )
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [
                _trusted(
                    {
                        "id": 14,
                        "created_at": "2026-07-09T00:00:01+00:00",
                        "body": f"{_CODERABBIT_MARKER}\n[P2] Something is actually broken here",
                    }
                )
            ],
        }
    )
    disabled_config = _config(auto_generated_markers=())

    result = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, disabled_config, client, 1, _lease(project_dir)
    )

    assert [item.source_comment_id for item in result.findings] == ["issue_comment:14"]
    assert result.findings[0].severity == "high"


def test_parse_pr_review_config_accepts_custom_auto_generated_marker() -> None:
    config = prw.parse_pr_review_config(
        {
            "pr_review": {
                "reviewer_allowlist": [{"app_slug": "codex-app"}],
                "auto_generated_markers": ["[[bot-summary]]"],
            }
        }
    )
    assert config.auto_generated_markers == ("[[bot-summary]]",)
    assert prw._is_auto_generated_comment("[[bot-summary]] High level notes", config) is True
    assert prw._is_auto_generated_comment("no marker here, High severity bug", config) is False


def test_finding_from_item_still_imports_explicit_high_without_auto_generated_marker() -> None:
    """Regression: explicit severity markers must still classify normally when no bot marker is present."""
    item = prw.ReviewItem(
        source="issue_comment",
        item_id="15",
        body="[HIGH] real bug here",
        created_at="2026-07-09T00:00:01+00:00",
        path=None,
        line=None,
        original_line=None,
        pull_request_review_id=None,
        raw=_trusted({"id": 15}),
    )
    finding = prw._finding_from_item(item, "issue_comment:15", _config(), 1)
    assert finding is not None
    assert finding.severity == "high"
    assert finding.needs_classification is False


def test_ev36_reviewer_allowlist_is_required() -> None:
    with pytest.raises(prw.ConfigError, match="reviewer_allowlist is required"):
        prw.parse_pr_review_config({"pr_review": {}})


def test_ev37_custom_severity_marker_regex_is_validated() -> None:
    with pytest.raises(prw.ConfigError, match="severity_markers"):
        prw.parse_pr_review_config(
            {
                "pr_review": {
                    "reviewer_allowlist": [{"app_slug": "codex-app"}],
                    "severity_markers": {"high": "["},
                }
            }
        )


def test_ev37_severity_parsing_and_classification_failsafe() -> None:
    config = _config()

    assert prw.classify_severity("P1: data loss", config).severity == "critical"
    must = prw.classify_severity("blocking until fixed", config)
    assert must.severity == "high"
    assert must.reason == "must_fix_marker"
    missing = prw.classify_severity("Please consider this edge case", config)
    assert missing.severity == "high"
    assert missing.needs_classification is True
    classified = prw.classify_severity(
        "No marker",
        config,
        classification_response="SEVERITY: medium\nCONFIDENCE: high\n",
    )
    assert classified.severity == "medium"
    assert classified.source == "external_classification"
    low_confidence = prw.classify_severity(
        "No marker",
        config,
        classification_response="SEVERITY: low\nCONFIDENCE: low\n",
    )
    assert low_confidence.severity == "high"
    assert low_confidence.source == "fail_safe"
    positive = prw.classify_severity(
        "No marker",
        config,
        classification_response="SEVERITY: none\nCONFIDENCE: high\n",
    )
    assert positive.severity is None
    assert positive.reason == "not_a_finding"


def test_apply_severity_classifications_persists_medium_in_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "baseline_review_id": 10,
            "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
            "processed_comment_ids": [],
            "findings": {},
        },
    )
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [
                _trusted(
                    {
                        "id": 12,
                        "created_at": "2026-07-09T00:00:01+00:00",
                        "body": "Please consider this edge case",
                    }
                )
            ],
        }
    )
    lease_token = _lease(project_dir)
    collected = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, _config(), client, 1, lease_token
    )

    applied = prw.apply_severity_classifications(
        "abcd1234-issue-1",
        project_dir,
        collected,
        _config(),
        {"issue_comment:12": "SEVERITY: medium\nCONFIDENCE: high\n"},
        1,
        lease_token,
    )

    state = lc.load_state("abcd1234-issue-1", project_dir)
    finding = applied.review_findings.findings[0]
    record = state.pr_review["findings"][finding.signature]
    assert finding.severity == "medium"
    assert finding.needs_classification is False
    assert record["severity"] == "medium"
    assert record["confirmed_severity"] == "medium"
    assert record["pending_classification_source_comment_ids"] == []
    assert "issue_comment:12" in applied.review_findings.processed_comment_ids
    assert "issue_comment:12" in state.pr_review["processed_comment_ids"]


def test_pending_classification_is_reimported_after_collect_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "baseline_review_id": 10,
            "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
            "processed_comment_ids": [],
            "findings": {},
        },
    )
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [
                _trusted(
                    {
                        "id": 12,
                        "created_at": "2026-07-09T00:00:01+00:00",
                        "body": "Please consider this edge case",
                    }
                )
            ],
        }
    )
    lease_token = _lease(project_dir)

    first = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, _config(), client, 1, lease_token
    )
    second = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, _config(), client, 1, lease_token
    )

    state = lc.load_state("abcd1234-issue-1", project_dir)
    signature = first.findings[0].signature
    assert first.needs_classification_count == 1
    assert second.needs_classification_count == 1
    assert second.findings[0].source_comment_id == "issue_comment:12"
    assert "issue_comment:12" not in second.processed_comment_ids
    assert state.pr_review["findings"][signature]["source_comment_ids"] == ["issue_comment:12"]


def test_apply_severity_classifications_drops_non_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "baseline_review_id": 10,
            "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
            "processed_comment_ids": [],
            "findings": {},
        },
    )
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [
                _trusted(
                    {
                        "id": 12,
                        "created_at": "2026-07-09T00:00:01+00:00",
                        "body": "Didn't find any major issues",
                    }
                )
            ],
        }
    )
    lease_token = _lease(project_dir)
    collected = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, _config(), client, 1, lease_token
    )

    applied = prw.apply_severity_classifications(
        "abcd1234-issue-1",
        project_dir,
        collected,
        _config(),
        {"issue_comment:12": "SEVERITY: none\nCONFIDENCE: high\n"},
        1,
        lease_token,
    )

    state = lc.load_state("abcd1234-issue-1", project_dir)
    phase_check = prw.phase_check_from_review_findings(applied.review_findings)
    assert applied.review_findings.findings == ()
    assert applied.review_findings.iteration_findings.signatures == frozenset()
    assert applied.classifications[0].severity is None
    assert state.pr_review["findings"] == {}
    assert phase_check.passed is True


def test_none_classification_preserves_existing_confirmed_finding() -> None:
    findings_map = {
        "sig-a": {
            "first_seen_iteration": 1,
            "last_seen_iteration": 1,
            "status": "open",
            "severity": "medium",
            "dismiss_reason": None,
            "source_comment_ids": ["review_comment:1"],
        }
    }
    pending = prw.ImportedFinding(
        "sig-a", "high", "review_comment:2", "informational", "app.py", 10, True
    )
    prw._upsert_finding(findings_map, pending, 2)

    prw._apply_classification_to_state(
        findings_map,
        pending,
        prw.SeverityDecision(None, "external_classification", False, "not_a_finding"),
        2,
    )

    assert findings_map["sig-a"]["severity"] == "medium"
    assert findings_map["sig-a"]["last_seen_iteration"] == 1
    assert findings_map["sig-a"]["source_comment_ids"] == ["review_comment:1"]


def test_phase_check_requires_response_for_medium_low_findings() -> None:
    result = prw.phase_check_from_review_findings(
        prw.ReviewFindingsResult(
            findings=(
                prw.ImportedFinding(
                    "sig-low", "low", "review_comment:1", "[P4] optional", "app.py", 10, False
                ),
            ),
            iteration_findings=lc.IterationFindings(frozenset({"sig-low"}), 1),
            previous_iteration_findings=lc.IterationFindings(frozenset(), 0),
            processed_comment_ids=("review_comment:1",),
            ignored_untrusted_comment_count=0,
            needs_classification_count=0,
        )
    )

    assert result.passed is False
    assert result.results[0].passed is False
    assert result.results[0].findings[0].severity == "low"


def test_repeated_finding_preserves_highest_severity() -> None:
    findings_map = {
        "sig-a": {
            "first_seen_iteration": 1,
            "last_seen_iteration": 1,
            "status": "open",
            "severity": "medium",
            "dismiss_reason": None,
            "source_comment_ids": ["review_comment:1"],
        }
    }

    prw._upsert_finding(
        findings_map,
        prw.ImportedFinding("sig-a", "critical", "review_comment:2", "[P1]", "app.py", 10, False),
        2,
    )
    prw._upsert_finding(
        findings_map,
        prw.ImportedFinding("sig-a", "low", "review_comment:3", "[P4]", "app.py", 10, False),
        3,
    )

    assert findings_map["sig-a"]["severity"] == "critical"
    assert findings_map["sig-a"]["last_seen_iteration"] == 3
    assert findings_map["sig-a"]["source_comment_ids"] == [
        "review_comment:1",
        "review_comment:2",
        "review_comment:3",
    ]


def test_ev38_only_medium_low_findings_can_be_dismissed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "processed_comment_ids": [],
            "findings": {
                "sig-medium": {
                    "first_seen_iteration": 1,
                    "last_seen_iteration": 1,
                    "status": "open",
                    "severity": "medium",
                    "dismiss_reason": None,
                    "source_comment_ids": ["review_comment:1"],
                },
                "sig-high": {
                    "first_seen_iteration": 1,
                    "last_seen_iteration": 1,
                    "status": "open",
                    "severity": "high",
                    "dismiss_reason": None,
                    "source_comment_ids": ["review_comment:2"],
                },
            },
        },
    )

    lease_token = _lease(project_dir)

    prw.dismiss_finding(
        "abcd1234-issue-1", project_dir, "sig-medium", "not actionable", lease_token
    )
    with pytest.raises(prw.DismissalError, match="cannot be dismissed"):
        prw.dismiss_finding("abcd1234-issue-1", project_dir, "sig-high", "skip", lease_token)

    state = lc.load_state("abcd1234-issue-1", project_dir)
    assert state.pr_review["findings"]["sig-medium"]["status"] == "dismissed"
    assert state.pr_review["findings"]["sig-medium"]["dismiss_reason"] == "not actionable"


def test_ev39_signature_normalizes_path_line_bucket_and_body_tokens() -> None:
    dedup = prw.DedupConfig()
    first = prw.ReviewItem(
        "review_comment",
        "1",
        "Please add null check https://example.test\n```python\nx()\n```\n---\nbot footer",
        "2026-07-09T00:00:00+00:00",
        "./src\\app.py",
        None,
        17,
        11,
        {},
    )
    same_bucket_same_tokens = prw.ReviewItem(
        "review_comment",
        "2",
        "A check for null should add",
        "2026-07-09T00:00:00+00:00",
        "src/app.py",
        19,
        None,
        11,
        {},
    )
    different_bucket = prw.ReviewItem(
        "review_comment",
        "3",
        "A check for null should add",
        "2026-07-09T00:00:00+00:00",
        "src/app.py",
        22,
        None,
        11,
        {},
    )

    assert prw.normalize_signature(first, dedup) == prw.normalize_signature(
        same_bucket_same_tokens, dedup
    )
    assert prw.normalize_signature(first, dedup) != prw.normalize_signature(different_bucket, dedup)


def test_ev40_no_progress_uses_reraised_signatures_or_non_decreasing_new_count() -> None:
    prev = prw.IterationFindings(frozenset({"sig-a"}), 2)

    reraised = prw.evaluate_no_progress(prev, prw.IterationFindings(frozenset({"sig-a"}), 1))
    non_decreasing = prw.evaluate_no_progress(prev, prw.IterationFindings(frozenset({"sig-b"}), 2))
    progress = prw.evaluate_no_progress(prev, prw.IterationFindings(frozenset({"sig-b"}), 1))

    assert reraised.no_progress is True
    assert reraised.reason == "reraised"
    assert non_decreasing.no_progress is True
    assert non_decreasing.reason == "new_count_non_decreasing"
    assert progress.no_progress is False


def test_ev40_loop_common_uses_pr_review_metadata_in_guard_path(tmp_path: Path) -> None:
    state = lc._initial_state(
        "loop", "issue-loop", "hash", str(tmp_path), "loop/issue-1", "pr_review_response"
    )
    phase_def = {
        "guards": {"max_iterations": 5, "no_progress": {"signature": "pr_review", "repeat": 2}},
        "on_failure": {"disposition": lc.Action.EXIT_FAILURE.value},
    }
    config = {"guards": {"max_iterations": 5, "no_progress": {"repeat": 2}}}

    first = prw.phase_check_from_review_findings(
        prw.ReviewFindingsResult(
            findings=(
                prw.ImportedFinding("sig-a", "high", "review_comment:1", "A", "app.py", 10, False),
            ),
            iteration_findings=lc.IterationFindings(frozenset({"sig-a"}), 1),
            previous_iteration_findings=lc.IterationFindings(frozenset(), 0),
            processed_comment_ids=("review_comment:1",),
            ignored_untrusted_comment_count=0,
            needs_classification_count=0,
        )
    )
    second = prw.phase_check_from_review_findings(
        prw.ReviewFindingsResult(
            findings=(
                prw.ImportedFinding("sig-a", "high", "review_comment:2", "A", "app.py", 10, False),
                prw.ImportedFinding("sig-b", "high", "review_comment:3", "B", "app.py", 20, False),
            ),
            iteration_findings=lc.IterationFindings(frozenset({"sig-a", "sig-b"}), 1),
            previous_iteration_findings=lc.IterationFindings(frozenset({"sig-a"}), 1),
            processed_comment_ids=("review_comment:2", "review_comment:3"),
            ignored_untrusted_comment_count=0,
            needs_classification_count=0,
        )
    )

    first_decision = lc.evaluate_guards(state, first, phase_def, config)
    second_decision = lc.evaluate_guards(state, second, phase_def, config)

    assert first_decision.disposition == "continue"
    assert first.signature != second.signature
    assert second_decision.disposition == lc.Action.EXIT_FAILURE.value
    assert second_decision.reason == "no_progress"

    state_without_phase_def = lc._initial_state(
        "loop-2", "issue-loop", "hash", str(tmp_path), "loop/issue-1", "pr_review_response"
    )
    lc.evaluate_guards(state_without_phase_def, first, None, config)
    fallback_decision = lc.evaluate_guards(state_without_phase_def, second, None, config)
    assert fallback_decision.reason == "no_progress"


def test_gh_api_client_uses_paginate_and_concatenates_array_pages(monkeypatch: Any) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout='[{"id": 1}]\n[{"id": 2}]')

    monkeypatch.setattr(prw.subprocess, "run", fake_run)

    result = prw.GhApiClient("owner/repo", sleeper=lambda _seconds: None).api("repos/o/r/items")

    assert result == [{"id": 1}, {"id": 2}]
    assert calls == [["gh", "api", "--paginate", "repos/o/r/items"]]


def test_gh_api_client_retries_nonzero_exit_with_backoff(monkeypatch: Any) -> None:
    attempts = [
        subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="rate limit"),
        subprocess.CompletedProcess(["gh"], 0, stdout="[]", stderr=""),
    ]
    sleeps: list[float] = []

    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return attempts.pop(0)

    monkeypatch.setattr(prw.subprocess, "run", fake_run)
    client = prw.GhApiClient(
        "owner/repo",
        max_retries=2,
        backoff_base_seconds=0.5,
        sleeper=lambda seconds: sleeps.append(seconds),
    )

    assert client.api("repos/o/r/items") == []
    assert sleeps == [0.5]


def test_gh_api_client_raises_for_invalid_json(monkeypatch: Any) -> None:
    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout="{not-json")

    monkeypatch.setattr(prw.subprocess, "run", fake_run)

    with pytest.raises(prw.GitHubApiError, match="invalid gh api JSON"):
        prw.GhApiClient("owner/repo", sleeper=lambda _seconds: None).api("repos/o/r/items")


def test_gh_api_client_raises_after_timeout_retries(monkeypatch: Any) -> None:
    def fake_run(_args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("gh", timeout=1)

    monkeypatch.setattr(prw.subprocess, "run", fake_run)

    with pytest.raises(prw.GitHubApiError, match="timed out"):
        prw.GhApiClient("owner/repo", max_retries=1, sleeper=lambda _seconds: None).api(
            "repos/o/r/items"
        )
