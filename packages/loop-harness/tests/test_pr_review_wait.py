"""PR review wait/import tests for loop-harness."""

from __future__ import annotations

import fcntl
import json
import subprocess
import threading
import time
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


def _activate_pending_review_action(project_dir: str, action_id: str = "action-1") -> None:
    state = lc.load_state("abcd1234-issue-1", project_dir)
    state.pending_action = lc.PendingAction(
        action_id,
        lc.Action.WAIT_EXTERNAL_REVIEW.value,
        state.phase,
        1,
        lc.now_iso(),
    )
    state.state_version += 1
    lc._write_state(state, project_dir)


def _empty_review_findings_result() -> prw.ReviewFindingsResult:
    return prw.ReviewFindingsResult(
        findings=(),
        iteration_findings=lc.IterationFindings(frozenset(), 0),
        previous_iteration_findings=lc.IterationFindings(frozenset(), 0),
        open_non_blocking=(),
        processed_comment_ids=(),
        ignored_untrusted_comment_count=0,
        needs_classification_count=0,
    )


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


def _coderabbit_config(
    *,
    alternate_reviewer: bool = False,
    checkruns: tuple[str, ...] = (),
    timeout_seconds: int = 60,
) -> prw.PrReviewConfig:
    reviewers = [
        prw.ReviewerAllowlistEntry(
            app_slug="coderabbitai",
            login="coderabbitai[bot]",
            user_type="Bot",
            author_associations=frozenset({"NONE"}),
        )
    ]
    if alternate_reviewer:
        reviewers.append(_config().reviewer_allowlist[0])
    return prw.PrReviewConfig(
        reviewer_allowlist=tuple(reviewers),
        checkrun_allowlist=frozenset(checkruns),
        poll_interval_seconds=1,
        timeout_seconds=timeout_seconds,
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


def _coderabbit_rate_limit_comment(
    *, trusted: bool = True, body: str | None = None, **extra: Any
) -> dict[str, Any]:
    data = {
        "id": 30,
        "created_at": "2026-07-09T00:00:01+00:00",
        "body": body
        if body is not None
        else (
            "<!-- This is an auto-generated reply by CodeRabbit -->\n"
            "Full review triggered. More reviews will be available in 52 minutes."
        ),
        **extra,
    }
    if not trusted:
        return _untrusted(data)
    return {
        **data,
        "user": {"login": "coderabbitai[bot]", "type": "Bot"},
        "author_association": "NONE",
        "performed_via_github_app": {"slug": "coderabbitai"},
    }


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


def test_record_baseline_reuses_injected_review_items_snapshot_without_refetching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DC3: `record_baseline` must reuse an injected `review_items` snapshot instead of
    fetching again. Before this fix, `_drain_before_push`'s drain-then-rebaseline flow made
    two *separate* fetches; a comment posted between them would be silently marked
    `processed` by `record_baseline`'s own (later) fetch without ever being imported as a
    finding by the drain's (earlier) fetch, permanently losing it. Sharing one snapshot
    across both calls closes that window."""
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
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [
                _trusted({"id": 20, "submitted_at": "2026-07-09T00:00:01+00:00", "body": "LGTM"})
            ],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [],
        }
    )

    review_items = prw.fetch_review_items(client, 12)
    assert client.calls == [
        "repos/owner/repo/pulls/12/reviews",
        "repos/owner/repo/pulls/12/comments",
        "repos/owner/repo/issues/12/comments",
    ]

    record = prw.record_baseline(
        "abcd1234-issue-1", project_dir, 12, client, lease_token, review_items=review_items
    )

    # No additional `gh api` round trips beyond the one snapshot fetch above.
    assert client.calls == [
        "repos/owner/repo/pulls/12/reviews",
        "repos/owner/repo/pulls/12/comments",
        "repos/owner/repo/issues/12/comments",
    ]
    assert record.baseline_review_id == 20
    assert "review:20" in record.processed_comment_ids


def test_record_baseline_uses_snapshot_captured_at_instead_of_write_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """code L3: when a caller reuses a pre-fetched `review_items` snapshot and passes its own
    fetch time via `snapshot_captured_at`, `record_baseline` must stamp `baseline_recorded_at`
    with that captured time, not with "now" at the (potentially much later) write time. Before
    this fix, a caller like `_drain_before_push()` that spends real time on severity
    classification between fetching the snapshot and calling this function would get a
    `baseline_recorded_at` stamped after that delay; a review/comment posted after the snapshot
    but before that later write has a `created_at` older than the new baseline, so
    `_is_importable()`'s `created_at > baseline_recorded_at` check would filter it out forever."""
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
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [
                _trusted({"id": 20, "submitted_at": "2026-07-09T00:00:01+00:00", "body": "LGTM"})
            ],
            "repos/owner/repo/pulls/12/comments": [],
            "repos/owner/repo/issues/12/comments": [],
        }
    )
    review_items = prw.fetch_review_items(client, 12)

    # Stands in for the (much later) real write time, e.g. after severity classification
    # finished -- it must never leak into `baseline_recorded_at` when `snapshot_captured_at`
    # is explicitly supplied.
    monkeypatch.setattr(lc, "now_iso", lambda: "2099-01-01T00:00:00+00:00")

    record = prw.record_baseline(
        "abcd1234-issue-1",
        project_dir,
        12,
        client,
        lease_token,
        review_items=review_items,
        snapshot_captured_at="2026-07-09T00:00:00.500000+00:00",
    )

    assert record.baseline_recorded_at == "2026-07-09T00:00:00.500000+00:00"
    state = lc.load_state("abcd1234-issue-1", project_dir)
    assert state.pr_review["baseline_recorded_at"] == "2026-07-09T00:00:00.500000+00:00"


def test_record_baseline_falls_back_to_now_when_snapshot_captured_at_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """code L3 complement: omitting `snapshot_captured_at` (the default, backward-compatible
    call shape used by callers with no pre-fetched snapshot to share) must preserve the
    original "stamp with now" behavior unchanged."""
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    client = FakeClient({"repos/owner/repo/pulls/12/reviews": []})
    monkeypatch.setattr(lc, "now_iso", lambda: "2026-07-09T12:00:00+00:00")

    record = prw.record_baseline("abcd1234-issue-1", project_dir, 12, client, lease_token)

    assert record.baseline_recorded_at == "2026-07-09T12:00:00+00:00"


def test_ev192_pre_rebaseline_collect_preserves_findings_across_next_record_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trusted review that arrives after an old baseline is not lost by the next re-baseline."""
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "baseline_review_id": 5,
            "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
            "processed_comment_ids": [],
            "findings": {},
        },
    )
    lease_token = _lease(project_dir)
    # A second reviewer (CodeRabbit) posts a CHANGES_REQUESTED review while the Maker is still
    # working on the previous iteration's findings, i.e. after the OLD baseline was recorded.
    late_review_comment = _trusted(
        {
            "id": 40,
            "created_at": "2026-07-09T00:00:05+00:00",
            "pull_request_review_id": 9,
            "body": "[P2] Please handle this edge case",
            "path": "app.py",
            "line": 12,
        }
    )
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [{"id": 9}],
            "repos/owner/repo/pulls/12/comments": [late_review_comment],
            "repos/owner/repo/issues/12/comments": [],
        }
    )

    # Pre-rebaseline drain: collect using the OLD (still baseline_review_id=5) baseline.
    result = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, _config(), client, 1, lease_token
    )
    assert [item.source_comment_id for item in result.findings] == ["review_comment:40"]
    state_after_collect = lc.load_state("abcd1234-issue-1", project_dir)
    assert "review_comment:40" in _findings_signature_source_ids(state_after_collect)

    # The Maker then pushes a fresh commit, and the orchestrator re-baselines. record_baseline
    # fetches the SAME reviews/comments (now including id=9 / review_comment:40) again and marks
    # them all "processed", but must not clobber the already-imported finding.
    prw.record_baseline("abcd1234-issue-1", project_dir, 12, client, lease_token)

    state_after_rebaseline = lc.load_state("abcd1234-issue-1", project_dir)
    assert state_after_rebaseline.pr_review["baseline_review_id"] == 9
    assert "review_comment:40" in _findings_signature_source_ids(state_after_rebaseline)


def _findings_signature_source_ids(state: lc.LoopState) -> set[str]:
    findings = state.pr_review.get("findings") if isinstance(state.pr_review, dict) else {}
    source_ids: set[str] = set()
    for record in (findings or {}).values():
        source_ids.update(record.get("source_comment_ids") or [])
    return source_ids


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


def test_review_completion_requires_commit_id_match_with_iteration_head() -> None:
    """DH4: a new `review_id` alone must not signal completion for *this* iteration -- GitHub
    allows submitting a review against a stale diff. Once `iteration_head_sha` is recorded,
    a trusted review whose `commit_id` is missing or does not match it must not be treated
    as a completion signal (fail-safe, not fail-open); only a review whose `commit_id`
    matches the just-pushed head counts."""
    stale_review = _trusted(
        {
            "id": 11,
            "state": "COMMENTED",
            "submitted_at": "2026-07-09T00:00:01+00:00",
            "body": "reviewed a stale diff",
            "commit_id": "stale-sha",
        }
    )
    stale_client = FakeClient({"repos/owner/repo/pulls/12/reviews": [stale_review]})

    stale_outcome = prw.wait_for_completion(
        12,
        {"baseline_review_id": 10, "iteration_head_sha": "head-sha"},
        _config(),
        stale_client,
        sleeper=lambda _seconds: None,
    )

    assert stale_outcome.signal == "timeout"
    assert stale_outcome.completed is False

    missing_commit_review = _trusted(
        {
            "id": 12,
            "state": "COMMENTED",
            "submitted_at": "2026-07-09T00:00:02+00:00",
            "body": "commit_id absent from payload",
        }
    )
    missing_client = FakeClient(
        {"repos/owner/repo/pulls/12/reviews": [stale_review, missing_commit_review]}
    )

    missing_outcome = prw.wait_for_completion(
        12,
        {"baseline_review_id": 10, "iteration_head_sha": "head-sha"},
        _config(),
        missing_client,
        sleeper=lambda _seconds: None,
    )

    assert missing_outcome.signal == "timeout"
    assert missing_outcome.completed is False

    matching_review = _trusted(
        {
            "id": 13,
            "state": "COMMENTED",
            "submitted_at": "2026-07-09T00:00:03+00:00",
            "body": "reviewed the just-pushed fix",
            "commit_id": "head-sha",
        }
    )
    matching_client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [
                stale_review,
                missing_commit_review,
                matching_review,
            ]
        }
    )

    matching_outcome = prw.wait_for_completion(
        12,
        {"baseline_review_id": 10, "iteration_head_sha": "head-sha"},
        _config(),
        matching_client,
        sleeper=lambda _seconds: None,
    )

    assert matching_outcome.signal == "review_submitted"
    assert matching_outcome.review_ids == (13,)


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


def _terminal_issue_comment(
    *, trusted: bool = True, sha: str = "abc1234def", body: str | None = None, **extra: Any
) -> dict[str, Any]:
    data = {
        "id": 20,
        "created_at": "2026-07-09T00:00:01+00:00",
        "body": body
        if body is not None
        else f"Didn't find any major issues. Reviewed commit: {sha}.",
        **extra,
    }
    return _trusted(data) if trusted else _untrusted(data)


def test_ev194_issue_comment_completion_signal_true_for_trusted_terminal_verdict() -> None:
    item = prw._review_item_from_issue_comment(_terminal_issue_comment())
    baseline = {
        "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
        "iteration_head_sha": "abc1234def",
    }

    assert prw._is_issue_comment_completion_signal(item, baseline, _config()) is True


def test_ev194_issue_comment_completion_signal_false_for_untrusted() -> None:
    item = prw._review_item_from_issue_comment(_terminal_issue_comment(trusted=False))
    baseline = {
        "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
        "iteration_head_sha": "abc1234def",
    }

    assert prw._is_issue_comment_completion_signal(item, baseline, _config()) is False


def test_ev194_issue_comment_completion_signal_false_before_baseline() -> None:
    item = prw._review_item_from_issue_comment(
        _terminal_issue_comment(**{"created_at": "2026-07-08T23:59:59+00:00"})
    )
    baseline = {
        "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
        "iteration_head_sha": "abc1234def",
    }

    assert prw._is_issue_comment_completion_signal(item, baseline, _config()) is False


def test_ev194_issue_comment_completion_signal_false_when_sha_mismatch() -> None:
    item = prw._review_item_from_issue_comment(_terminal_issue_comment(sha="ffffffffff"))
    baseline = {
        "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
        "iteration_head_sha": "abc1234def",
    }

    assert prw._is_issue_comment_completion_signal(item, baseline, _config()) is False


def test_ev194_issue_comment_completion_signal_false_when_pattern_does_not_match() -> None:
    item = prw._review_item_from_issue_comment(
        _terminal_issue_comment(body="Still reviewing, will report back shortly.")
    )
    baseline = {
        "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
        "iteration_head_sha": "abc1234def",
    }

    assert prw._is_issue_comment_completion_signal(item, baseline, _config()) is False


def test_ev194_issue_comment_completion_signal_false_for_auto_generated_reply() -> None:
    item = prw._review_item_from_issue_comment(
        _terminal_issue_comment(
            body="<!-- This is an auto-generated reply by CodeRabbit -->\nLGTM, rate limited, retrying."
        )
    )
    baseline = {
        "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
        "iteration_head_sha": "abc1234def",
    }

    assert prw._is_issue_comment_completion_signal(item, baseline, _config()) is False


def test_wait_for_completion_returns_issue_comment_completed_for_trusted_terminal_comment() -> None:
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/issues/12/comments": [_terminal_issue_comment()],
        }
    )
    baseline = {
        "baseline_review_id": 10,
        "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
        "iteration_head_sha": "abc1234def",
    }

    outcome = prw.wait_for_completion(
        12, baseline, _config(), client, sleeper=lambda _seconds: None
    )

    assert outcome.signal == "issue_comment_completed"
    assert outcome.completed is True
    assert outcome.issue_comment_ids == ("issue_comment:20",)


def test_wait_for_completion_ignores_processed_terminal_issue_comment() -> None:
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/issues/12/comments": [_terminal_issue_comment()],
        }
    )
    baseline = {
        "baseline_review_id": 10,
        "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
        "iteration_head_sha": "abc1234def",
        "processed_comment_ids": ["issue_comment:20"],
    }

    outcome = prw.wait_for_completion(
        12, baseline, _config(), client, sleeper=lambda _seconds: None
    )

    assert outcome.signal == "timeout"


def test_wait_for_completion_ignores_rate_limited_issue_comment_reply() -> None:
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/issues/12/comments": [
                _terminal_issue_comment(
                    body="<!-- This is an auto-generated reply by CodeRabbit -->\nLGTM, rate limited."
                )
            ],
        }
    )
    baseline = {
        "baseline_review_id": 10,
        "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
        "iteration_head_sha": "abc1234def",
    }

    outcome = prw.wait_for_completion(
        12, baseline, _config(), client, sleeper=lambda _seconds: None
    )

    assert outcome.signal == "timeout"


def test_reviewer_unavailable_requires_trusted_post_baseline_unprocessed_comment() -> None:
    config = _coderabbit_config()
    baseline = {
        "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
        "processed_comment_ids": [],
    }

    assert prw._is_reviewer_unavailable_signal(
        prw._review_item_from_issue_comment(_coderabbit_rate_limit_comment()),
        baseline,
        config,
    )
    assert not prw._is_reviewer_unavailable_signal(
        prw._review_item_from_issue_comment(_coderabbit_rate_limit_comment(trusted=False)),
        baseline,
        config,
    )
    assert not prw._is_reviewer_unavailable_signal(
        prw._review_item_from_issue_comment(
            _coderabbit_rate_limit_comment(created_at="2026-07-08T23:59:59+00:00")
        ),
        baseline,
        config,
    )
    assert not prw._is_reviewer_unavailable_signal(
        prw._review_item_from_issue_comment(_coderabbit_rate_limit_comment()),
        {**baseline, "processed_comment_ids": ["issue_comment:30"]},
        config,
    )


@pytest.mark.parametrize(
    "body",
    [
        "<!-- This is an auto-generated reply by CodeRabbit -->\nReview queued.",
        "Full review triggered. More reviews will be available in 52 minutes.",
        (
            "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
            "The rate limit section changed in this pull request."
        ),
        (
            "<!-- This is an auto-generated comment: review in progress by coderabbit.ai -->\n"
            "Waiting because of a rate limit."
        ),
    ],
)
def test_reviewer_unavailable_requires_specific_marker_and_rate_limit_phrase(
    body: str,
) -> None:
    item = prw._review_item_from_issue_comment(_coderabbit_rate_limit_comment(body=body))
    baseline = {"baseline_recorded_at": "2026-07-09T00:00:00+00:00"}

    assert not prw._is_reviewer_unavailable_signal(item, baseline, _coderabbit_config())


def test_wait_returns_reviewer_unavailable_immediately_when_coderabbit_is_only_path() -> None:
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/issues/12/comments": [_coderabbit_rate_limit_comment()],
        }
    )
    baseline = {
        "baseline_review_id": 10,
        "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
        "processed_comment_ids": [],
    }
    heartbeats: list[str] = []

    outcome = prw.wait_for_completion(
        12,
        baseline,
        _coderabbit_config(),
        client,
        sleeper=lambda _seconds: None,
        heartbeat=lambda: heartbeats.append("beat"),
    )
    phase_check = prw.phase_check_from_completion_outcome(outcome)

    assert outcome.signal == "reviewer_unavailable"
    assert outcome.completed is True
    assert outcome.reviewer_unavailable_reason == "rate_limited"
    assert outcome.reviewer_unavailable_comment_ids == ("issue_comment:30",)
    assert heartbeats == []
    assert phase_check.signature == "external_reviewer_unavailable"
    assert phase_check.metadata == {
        "reviewer_unavailable_comment_ids": ["issue_comment:30"],
        "reviewer_unavailable_reason": "rate_limited",
    }
    assert "Full review triggered" not in str(phase_check.metadata)


def test_wait_keeps_polling_for_slow_alternate_reviewer_before_handoff() -> None:
    review_calls = 0

    class SlowCodexClient(FakeClient):
        def api(self, path: str) -> Any:
            nonlocal review_calls
            if path == "repos/owner/repo/pulls/12/reviews":
                review_calls += 1
                if review_calls == 2:
                    return [
                        _trusted(
                            {
                                "id": 11,
                                "state": "COMMENTED",
                                "submitted_at": "2026-07-09T00:00:02+00:00",
                                "body": "Codex review complete",
                            }
                        )
                    ]
                return []
            return super().api(path)

    client = SlowCodexClient(
        {"repos/owner/repo/issues/12/comments": [_coderabbit_rate_limit_comment()]}
    )
    times = iter([0.0, 0.0])
    heartbeats: list[str] = []

    outcome = prw.wait_for_completion(
        12,
        {
            "baseline_review_id": 10,
            "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
        },
        _coderabbit_config(alternate_reviewer=True),
        client,
        monotonic=lambda: next(times),
        sleeper=lambda _seconds: None,
        heartbeat=lambda: heartbeats.append("beat"),
    )

    assert outcome.signal == "review_submitted"
    assert outcome.review_ids == (11,)
    assert heartbeats == ["beat"]


@pytest.mark.parametrize(
    "config",
    [
        _coderabbit_config(alternate_reviewer=True, timeout_seconds=1),
        _coderabbit_config(checkruns=("Codex Review",), timeout_seconds=1),
    ],
)
def test_wait_converts_timeout_to_reviewer_unavailable_with_alternate_path(
    config: prw.PrReviewConfig,
) -> None:
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/issues/12/comments": [_coderabbit_rate_limit_comment()],
            "repos/owner/repo/commits/abc/check-runs": {
                "check_runs": [{"name": "Codex Review", "status": "in_progress"}]
            },
        }
    )
    times = iter([0.0, 0.0, 2.0])
    heartbeats: list[str] = []

    outcome = prw.wait_for_completion(
        12,
        {
            "baseline_review_id": 10,
            "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
            "iteration_head_sha": "abc",
        },
        config,
        client,
        monotonic=lambda: next(times),
        sleeper=lambda _seconds: None,
        heartbeat=lambda: heartbeats.append("beat"),
    )

    assert outcome.signal == "reviewer_unavailable"
    assert outcome.completed is True
    assert outcome.timed_out is False
    assert heartbeats == ["beat"]


def test_wait_for_completion_ignores_untrusted_issue_comment_terminal_verdict() -> None:
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/issues/12/comments": [_terminal_issue_comment(trusted=False)],
        }
    )
    baseline = {
        "baseline_review_id": 10,
        "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
        "iteration_head_sha": "abc1234def",
    }

    outcome = prw.wait_for_completion(
        12, baseline, _config(), client, sleeper=lambda _seconds: None
    )

    assert outcome.signal == "timeout"


def test_wait_for_completion_still_reaches_checkrun_when_issue_comments_are_not_terminal() -> None:
    client = FakeClient(
        {
            "repos/owner/repo/pulls/12/reviews": [],
            "repos/owner/repo/issues/12/comments": [
                _terminal_issue_comment(body="Still working on this, more soon.")
            ],
            "repos/owner/repo/commits/abc1234def/check-runs": {
                "check_runs": [{"name": "ci", "status": "completed"}]
            },
        }
    )
    baseline = {
        "baseline_review_id": 10,
        "baseline_recorded_at": "2026-07-09T00:00:00+00:00",
        "iteration_head_sha": "abc1234def",
    }

    outcome = prw.wait_for_completion(
        12, baseline, _config(checkruns=("ci",)), client, sleeper=lambda _seconds: None
    )

    assert outcome.signal == "check_run_completed"


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

    lease_token = _lease(project_dir)
    result = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, _config(), client, 1, lease_token
    )
    state = lc.load_state("abcd1234-issue-1", project_dir)
    journal = Path(project_dir) / ".claude" / "loop" / "abcd1234-issue-1" / "journal.jsonl"
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]

    assert [item.source_comment_id for item in result.findings] == [
        "review_comment:3",
        "issue_comment:3",
    ]
    # DC4: explicit-severity findings are deliberately *not* marked processed by
    # `collect_review_findings` itself -- only `confirm_review_findings_reported` (called
    # after the caller has durably captured `result`) does, so a crash before that
    # confirmation safely re-surfaces them on retry instead of silently dropping them.
    assert "review_comment:3" not in result.processed_comment_ids
    assert "issue_comment:3" not in result.processed_comment_ids
    assert result.ignored_untrusted_comment_count == 1
    assert state.ignored_untrusted_comment_count == 1
    ignored_events = [event for event in events if event["event"] == "ignored_untrusted_comment"]
    assert ignored_events[0]["payload"]["comment_id"] == "4"
    assert ignored_events[0]["payload"]["notification_required"] is True

    prw.confirm_review_findings_reported("abcd1234-issue-1", project_dir, result, lease_token)
    confirmed_state = lc.load_state("abcd1234-issue-1", project_dir)
    assert "review_comment:3" in confirmed_state.pr_review["processed_comment_ids"]
    assert "issue_comment:3" in confirmed_state.pr_review["processed_comment_ids"]


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


def test_collect_review_findings_skips_terminal_issue_comment(
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
            "repos/owner/repo/issues/12/comments": [_terminal_issue_comment()],
        }
    )

    result = prw.collect_review_findings(
        "abcd1234-issue-1", project_dir, 12, _config(), client, 1, _lease(project_dir)
    )

    assert result.findings == ()
    assert result.needs_classification_count == 0
    assert "issue_comment:20" in result.processed_comment_ids


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


def test_review_findings_snapshot_preserves_explicit_finding_across_process_boundary(
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
                        "body": "[HIGH] Explicit blocker",
                    }
                ),
                _trusted(
                    {
                        "id": 13,
                        "created_at": "2026-07-09T00:00:02+00:00",
                        "body": "Please consider this edge case",
                    }
                ),
            ],
        }
    )
    lease_token = _lease(project_dir)
    _activate_pending_review_action(project_dir)
    collected = prw.collect_review_findings(
        "abcd1234-issue-1",
        project_dir,
        12,
        _config(),
        client,
        1,
        lease_token,
        action_id="action-1",
    )

    artifact_path = prw.save_review_findings_snapshot(
        "abcd1234-issue-1", project_dir, "action-1", collected, lease_token
    )
    # DC4: the explicit-severity finding (`issue_comment:12`) is only marked processed once
    # the caller confirms it has durably captured `collected` -- mirroring how
    # `loop_driver` calls this right after `save_review_findings_snapshot` succeeds.
    prw.confirm_review_findings_reported(
        "abcd1234-issue-1", project_dir, collected, lease_token, action_id="action-1"
    )
    second_collect = prw.collect_review_findings(
        "abcd1234-issue-1",
        project_dir,
        12,
        _config(),
        client,
        1,
        lease_token,
        action_id="action-1",
    )
    restored = prw.load_review_findings_snapshot(
        "abcd1234-issue-1", project_dir, "action-1", lease_token
    )
    applied = prw.apply_severity_classifications(
        "abcd1234-issue-1",
        project_dir,
        restored,
        _config(),
        {"issue_comment:13": "SEVERITY: medium\nCONFIDENCE: high\n"},
        1,
        lease_token,
        action_id="action-1",
    )
    phase_check = prw.phase_check_from_review_findings(applied.review_findings)

    assert artifact_path == "artifacts/action-1/review_findings.json"
    assert {item.source_comment_id for item in collected.findings} == {
        "issue_comment:12",
        "issue_comment:13",
    }
    assert [item.source_comment_id for item in second_collect.findings] == ["issue_comment:13"]
    assert restored == collected
    assert {item.source_comment_id for item in applied.review_findings.findings} == {
        "issue_comment:12",
        "issue_comment:13",
    }
    assert phase_check.passed is False
    assert {item.severity for item in phase_check.results[0].findings} == {"high", "medium"}


def test_review_findings_snapshot_uses_0600_and_redacts_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    _activate_pending_review_action(project_dir)
    result = prw.ReviewFindingsResult(
        findings=(
            prw.ImportedFinding(
                "sig-a",
                "high",
                "review_comment:1",
                "api_key: sensitive-value-xyz",
                "token=another-secret-xyz",
                10,
                False,
            ),
        ),
        iteration_findings=lc.IterationFindings(frozenset({"sig-a"}), 1),
        previous_iteration_findings=lc.IterationFindings(frozenset(), 0),
        open_non_blocking=(),
        processed_comment_ids=("review_comment:1",),
        ignored_untrusted_comment_count=0,
        needs_classification_count=0,
    )

    prw.save_review_findings_snapshot(
        "abcd1234-issue-1", project_dir, "action-1", result, lease_token
    )

    path = lc.artifact_path("abcd1234-issue-1", project_dir, "action-1", "review_findings.json")
    restored = prw.load_review_findings_snapshot(
        "abcd1234-issue-1", project_dir, "action-1", lease_token
    )
    assert path.stat().st_mode & 0o777 == 0o600
    assert "sensitive-value-xyz" not in path.read_text(encoding="utf-8")
    assert "another-secret-xyz" not in path.read_text(encoding="utf-8")
    assert restored.findings[0].body_excerpt == "[REDACTED]"
    assert restored.findings[0].path == "[REDACTED]"


def test_save_review_findings_snapshot_rejects_oversized_serialized_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    _activate_pending_review_action(project_dir)
    result = prw.ReviewFindingsResult(
        findings=(
            prw.ImportedFinding(
                "sig-a",
                "high",
                "review_comment:1",
                "x" * prw.MAX_REVIEW_FINDINGS_SNAPSHOT_BYTES,
                "app.py",
                10,
                False,
            ),
        ),
        iteration_findings=lc.IterationFindings(frozenset({"sig-a"}), 1),
        previous_iteration_findings=lc.IterationFindings(frozenset(), 0),
        open_non_blocking=(),
        processed_comment_ids=("review_comment:1",),
        ignored_untrusted_comment_count=0,
        needs_classification_count=0,
    )
    path = lc.artifact_path("abcd1234-issue-1", project_dir, "action-1", "review_findings.json")

    with pytest.raises(prw.PrReviewWaitError, match="artifact exceeds size limit"):
        prw.save_review_findings_snapshot(
            "abcd1234-issue-1", project_dir, "action-1", result, lease_token
        )

    assert not path.exists()


def test_load_review_findings_snapshot_fails_closed_when_artifact_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    _activate_pending_review_action(project_dir, "missing-action")

    with pytest.raises(prw.PrReviewWaitError, match="snapshot artifact is missing"):
        prw.load_review_findings_snapshot(
            "abcd1234-issue-1", project_dir, "missing-action", lease_token
        )


def test_load_review_findings_snapshot_fails_closed_for_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    _activate_pending_review_action(project_dir)
    lc.save_artifact("abcd1234-issue-1", project_dir, "action-1", "review_findings.json", "{")

    with pytest.raises(prw.PrReviewWaitError, match="malformed JSON"):
        prw.load_review_findings_snapshot("abcd1234-issue-1", project_dir, "action-1", lease_token)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("schema_version", prw.REVIEW_FINDINGS_SNAPSHOT_SCHEMA_VERSION + 1),
        ("schema_version", True),
        ("findings", "not-a-list"),
        ("processed_comment_ids", [1]),
        ("needs_classification_count", "0"),
    ],
)
def test_load_review_findings_snapshot_fails_closed_for_invalid_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    invalid_value: Any,
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    _activate_pending_review_action(project_dir)
    result = prw.ReviewFindingsResult(
        findings=(),
        iteration_findings=lc.IterationFindings(frozenset(), 0),
        previous_iteration_findings=lc.IterationFindings(frozenset(), 0),
        open_non_blocking=(),
        processed_comment_ids=(),
        ignored_untrusted_comment_count=0,
        needs_classification_count=0,
    )
    payload = {
        **prw._review_findings_snapshot_dict(result),
        "loop_id": "abcd1234-issue-1",
        "action_id": "action-1",
        field: invalid_value,
    }
    lc.save_artifact(
        "abcd1234-issue-1",
        project_dir,
        "action-1",
        "review_findings.json",
        json.dumps(payload),
    )

    with pytest.raises(prw.PrReviewWaitError, match="invalid review findings snapshot"):
        prw.load_review_findings_snapshot("abcd1234-issue-1", project_dir, "action-1", lease_token)


def test_load_review_findings_snapshot_accepts_legacy_v1_payload_without_open_non_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR#228 review: a v1 snapshot (pre-#213, no `open_non_blocking` key) persisted by an
    in-flight loop before this package upgraded must still be readable, defaulting
    `open_non_blocking` to `()` -- failing closed here would strand that loop unable to
    resume/classify."""
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    _activate_pending_review_action(project_dir)
    payload = {
        **prw._review_findings_snapshot_dict(_empty_review_findings_result()),
        "loop_id": "abcd1234-issue-1",
        "action_id": "action-1",
        "schema_version": 1,
    }
    del payload["open_non_blocking"]
    lc.save_artifact(
        "abcd1234-issue-1", project_dir, "action-1", "review_findings.json", json.dumps(payload)
    )

    restored = prw.load_review_findings_snapshot(
        "abcd1234-issue-1", project_dir, "action-1", lease_token
    )

    assert restored.open_non_blocking == ()


def test_load_review_findings_snapshot_rejects_v1_payload_with_open_non_blocking_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `schema_version: 1` payload that *does* carry an `open_non_blocking` key is still
    rejected -- v1's own key set stays exact; only its *absence* is tolerated."""
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    _activate_pending_review_action(project_dir)
    payload = {
        **prw._review_findings_snapshot_dict(_empty_review_findings_result()),
        "loop_id": "abcd1234-issue-1",
        "action_id": "action-1",
        "schema_version": 1,
    }
    lc.save_artifact(
        "abcd1234-issue-1", project_dir, "action-1", "review_findings.json", json.dumps(payload)
    )

    with pytest.raises(prw.PrReviewWaitError, match="invalid review findings snapshot"):
        prw.load_review_findings_snapshot("abcd1234-issue-1", project_dir, "action-1", lease_token)


def test_load_review_findings_snapshot_rejects_unsupported_future_schema_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema version newer than what this package knows how to read (e.g. 3) must fail
    closed, not silently coerce to the current shape."""
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    _activate_pending_review_action(project_dir)
    payload = {
        **prw._review_findings_snapshot_dict(_empty_review_findings_result()),
        "loop_id": "abcd1234-issue-1",
        "action_id": "action-1",
        "schema_version": 3,
    }
    lc.save_artifact(
        "abcd1234-issue-1", project_dir, "action-1", "review_findings.json", json.dumps(payload)
    )

    with pytest.raises(prw.PrReviewWaitError, match="unsupported schema_version"):
        prw.load_review_findings_snapshot("abcd1234-issue-1", project_dir, "action-1", lease_token)


@pytest.mark.parametrize("field_name", ["iteration_findings", "previous_iteration_findings"])
def test_load_review_findings_snapshot_rejects_new_count_without_signatures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field_name: str
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    _activate_pending_review_action(project_dir)
    prw.save_review_findings_snapshot(
        "abcd1234-issue-1",
        project_dir,
        "action-1",
        _empty_review_findings_result(),
        lease_token,
    )
    content = lc.load_artifact("abcd1234-issue-1", project_dir, "action-1", "review_findings.json")
    assert content is not None
    payload = json.loads(content)
    payload[field_name] = {"signatures": [], "new_count": 1}
    lc.save_artifact(
        "abcd1234-issue-1",
        project_dir,
        "action-1",
        "review_findings.json",
        json.dumps(payload),
    )

    with pytest.raises(prw.PrReviewWaitError, match="new_count exceeds signatures"):
        prw.load_review_findings_snapshot("abcd1234-issue-1", project_dir, "action-1", lease_token)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [("loop_id", "other-loop"), ("action_id", "other-action")],
)
def test_load_review_findings_snapshot_rejects_mismatched_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    invalid_value: str,
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    _activate_pending_review_action(project_dir)
    prw.save_review_findings_snapshot(
        "abcd1234-issue-1",
        project_dir,
        "action-1",
        _empty_review_findings_result(),
        lease_token,
    )
    content = lc.load_artifact("abcd1234-issue-1", project_dir, "action-1", "review_findings.json")
    assert content is not None
    payload = {**json.loads(content), field: invalid_value}
    lc.save_artifact(
        "abcd1234-issue-1",
        project_dir,
        "action-1",
        "review_findings.json",
        json.dumps(payload),
    )

    with pytest.raises(prw.PrReviewWaitError, match="does not match"):
        prw.load_review_findings_snapshot("abcd1234-issue-1", project_dir, "action-1", lease_token)


@pytest.mark.parametrize("operation", ["save", "load"])
@pytest.mark.parametrize(
    ("boundary", "expected_exception"),
    [
        ("pending_none", lc.StaleActionError),
        ("stale_action_id", lc.StaleActionError),
        ("phase_mismatch", lc.StaleActionError),
        ("wrong_action", lc.ProtocolViolationError),
        ("invalid_lease", lc.WriteRejectedError),
    ],
)
def test_review_findings_snapshot_access_requires_active_action_and_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    boundary: str,
    expected_exception: type[Exception],
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    _activate_pending_review_action(project_dir)
    result = _empty_review_findings_result()
    if operation == "load":
        prw.save_review_findings_snapshot(
            "abcd1234-issue-1", project_dir, "action-1", result, lease_token
        )

    state = lc.load_state("abcd1234-issue-1", project_dir)
    assert state.pending_action is not None
    if boundary == "pending_none":
        state.pending_action = None
    elif boundary == "stale_action_id":
        state.pending_action.action_id = "other-action"
    elif boundary == "phase_mismatch":
        state.pending_action.phase = "implementation"
    elif boundary == "wrong_action":
        state.pending_action.action = lc.Action.RUN_MAKER.value
    lc._write_state(state, project_dir)
    access_lease = "invalid-lease" if boundary == "invalid_lease" else lease_token

    with pytest.raises(expected_exception):
        if operation == "save":
            prw.save_review_findings_snapshot(
                "abcd1234-issue-1", project_dir, "action-1", result, access_lease
            )
        else:
            prw.load_review_findings_snapshot(
                "abcd1234-issue-1", project_dir, "action-1", access_lease
            )


@pytest.mark.parametrize("boundary", ["symlink", "mode", "size", "no_follow"])
def test_load_review_findings_snapshot_rejects_unsafe_artifact_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    project_dir = _setup_state(tmp_path, monkeypatch)
    lease_token = _lease(project_dir)
    _activate_pending_review_action(project_dir)
    prw.save_review_findings_snapshot(
        "abcd1234-issue-1",
        project_dir,
        "action-1",
        _empty_review_findings_result(),
        lease_token,
    )
    path = lc.artifact_path("abcd1234-issue-1", project_dir, "action-1", "review_findings.json")
    if boundary == "symlink":
        source = tmp_path / "snapshot-source.json"
        source.write_bytes(path.read_bytes())
        source.chmod(0o600)
        path.unlink()
        path.symlink_to(source)
    elif boundary == "mode":
        path.chmod(0o644)
    elif boundary == "size":
        path.write_bytes(b" " * (prw.MAX_REVIEW_FINDINGS_SNAPSHOT_BYTES + 1))
        path.chmod(0o600)
    else:
        monkeypatch.delattr(prw.os, "O_NOFOLLOW", raising=False)

    with pytest.raises(prw.PrReviewWaitError, match="invalid review findings snapshot"):
        prw.load_review_findings_snapshot("abcd1234-issue-1", project_dir, "action-1", lease_token)


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
                        "body": "This comment is informational only.",
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


def test_phase_check_passes_for_medium_low_only_findings_and_reports_non_blocking_open() -> None:
    """issue #213/B: a medium/low-only result must be `passed=true` -- otherwise
    `pr_review_response` has no exit path once every blocking finding is resolved but nobody
    explicitly dismissed a low nitpick (deadlock). `findings` still carries the low severity
    for observability, and `non_blocking_open` metadata reports it for exit-time reporting."""
    non_blocking = prw.NonBlockingFinding(
        signature="sig-low",
        severity="low",
        path="app.py",
        line=10,
        body_excerpt="[P4] optional",
    )
    result = prw.phase_check_from_review_findings(
        prw.ReviewFindingsResult(
            findings=(
                prw.ImportedFinding(
                    "sig-low", "low", "review_comment:1", "[P4] optional", "app.py", 10, False
                ),
            ),
            iteration_findings=lc.IterationFindings(frozenset(), 0),
            previous_iteration_findings=lc.IterationFindings(frozenset(), 0),
            open_non_blocking=(non_blocking,),
            processed_comment_ids=("review_comment:1",),
            ignored_untrusted_comment_count=0,
            needs_classification_count=0,
        )
    )

    assert result.passed is True
    assert result.results[0].passed is True
    assert result.results[0].findings[0].severity == "low"
    assert result.metadata["non_blocking_open"] == [
        {
            "signature": "sig-low",
            "severity": "low",
            "path": "app.py",
            "line": 10,
            "body_excerpt": "[P4] optional",
        }
    ]


def test_phase_check_mixed_high_and_low_blocks_only_until_high_resolves() -> None:
    """issue #213: a re-raised low alongside a newly-raised high must still block
    (`passed=false`) -- only blocking severities gate `passed`, but `findings` keeps every
    severity for observability. Once the high finding resolves and only the low remains,
    `passed` flips to true (issue #213/B) while the low is still reported via
    `non_blocking_open`."""
    low = prw.NonBlockingFinding(
        signature="sig-low", severity="low", path="app.py", line=5, body_excerpt="nit"
    )
    mixed = prw.phase_check_from_review_findings(
        prw.ReviewFindingsResult(
            findings=(
                prw.ImportedFinding(
                    "sig-low", "low", "review_comment:1", "nit", "app.py", 5, False
                ),
                prw.ImportedFinding(
                    "sig-high", "high", "review_comment:2", "fix", "app.py", 20, False
                ),
            ),
            iteration_findings=lc.IterationFindings(frozenset({"sig-high"}), 1),
            previous_iteration_findings=lc.IterationFindings(frozenset(), 0),
            open_non_blocking=(low,),
            processed_comment_ids=("review_comment:1", "review_comment:2"),
            ignored_untrusted_comment_count=0,
            needs_classification_count=0,
        )
    )

    assert mixed.passed is False
    assert mixed.results[0].passed is False
    assert {item.severity for item in mixed.results[0].findings} == {"low", "high"}

    resolved = prw.phase_check_from_review_findings(
        prw.ReviewFindingsResult(
            findings=(
                prw.ImportedFinding(
                    "sig-low", "low", "review_comment:1", "nit", "app.py", 5, False
                ),
            ),
            iteration_findings=lc.IterationFindings(frozenset(), 0),
            previous_iteration_findings=lc.IterationFindings(frozenset({"sig-high"}), 1),
            open_non_blocking=(low,),
            processed_comment_ids=("review_comment:1",),
            ignored_untrusted_comment_count=0,
            needs_classification_count=0,
        )
    )

    assert resolved.passed is True
    assert resolved.metadata["non_blocking_open"][0]["signature"] == "sig-low"


def test_phase_check_fail_safe_high_from_unclassified_finding_blocks() -> None:
    """issue #213: `classify_severity`'s existing fail-safe-to-`high` behavior (empty/invalid/
    low-confidence classification response, code review B7) must still result in a blocking
    `passed=false` once threaded through `phase_check_from_review_findings` -- fail-safe
    findings are never silently downgraded to non-blocking."""
    config = _config()

    missing_response = prw.classify_severity("Please consider this edge case", config)
    invalid_response = prw.classify_severity(
        "Please consider this edge case", config, classification_response="not a valid response"
    )
    low_confidence = prw.classify_severity(
        "Please consider this edge case",
        config,
        classification_response="SEVERITY: medium\nCONFIDENCE: low\n",
    )

    for decision in (missing_response, invalid_response, low_confidence):
        assert decision.severity == "high"
        assert decision.source == "fail_safe"
        result = prw.phase_check_from_review_findings(
            prw.ReviewFindingsResult(
                findings=(
                    prw.ImportedFinding(
                        "sig-a",
                        decision.severity,
                        "review_comment:1",
                        "Please consider this edge case",
                        "app.py",
                        1,
                        False,
                    ),
                ),
                iteration_findings=lc.IterationFindings(frozenset({"sig-a"}), 1),
                previous_iteration_findings=lc.IterationFindings(frozenset(), 0),
                open_non_blocking=(),
                processed_comment_ids=("review_comment:1",),
                ignored_untrusted_comment_count=0,
                needs_classification_count=0,
            )
        )
        assert result.passed is False


def test_open_non_blocking_findings_normalizes_multiline_body_excerpt() -> None:
    """PR#228 review: a multi-line reviewer comment must not leak newlines into
    `NonBlockingFinding.body_excerpt` -- consumers (e.g. `loop_driver._exit_success_comment()`)
    render it as a single Markdown bullet line, and an embedded newline would corrupt that.
    Whitespace normalization happens *before* the 200-char truncation."""
    findings_map = {
        "sig-a": {
            "status": "open",
            "severity": "low",
            "path": "app.py",
            "line": 5,
            "body_excerpt": "line one\n\n   line two  \nline three",
        }
    }

    result = prw._open_non_blocking_findings(findings_map)

    assert len(result) == 1
    assert result[0].body_excerpt == "line one line two line three"
    assert "\n" not in result[0].body_excerpt


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


def test_upsert_finding_reopens_addressed_record_on_reraise() -> None:
    """Issue #235 reraise-after-addressed: a signature `mark_addressed_findings()` already
    marked "addressed" must go back to `status == "open"` -- and drop its now-stale
    `addressed_at_*` markers -- the moment the same signature is imported again, so the
    exit-comment matrix (`_finding_status_label`) stops reporting a currently-blocking finding
    as resolved and a later genuine fix can update `mark_addressed_findings()`'s
    `status == "open"`-gated record instead of being silently skipped."""
    findings_map = {
        "sig-a": {
            "first_seen_iteration": 1,
            "last_seen_iteration": 1,
            "status": "addressed",
            "severity": "high",
            "confirmed_severity": "high",
            "dismiss_reason": None,
            "pending_classification_source_comment_ids": [],
            "source_comment_ids": ["review_comment:1"],
            "addressed_at_commit": "cafebabecafebabe",
            "addressed_at_iteration": 2,
            "path": "app.py",
            "line": 10,
        }
    }

    prw._upsert_finding(
        findings_map,
        prw.ImportedFinding("sig-a", "high", "review_comment:2", "[P1]", "app.py", 10, False),
        3,
    )

    record = findings_map["sig-a"]
    assert record["status"] == "open"
    assert record["addressed_at_commit"] is None
    assert record["addressed_at_iteration"] is None
    assert record["last_seen_iteration"] == 3
    assert record["source_comment_ids"] == ["review_comment:1", "review_comment:2"]


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


def test_ev40_no_progress_requires_identical_blocking_signature_set() -> None:
    """issue #213/A: only an *exact* re-raise of the same (non-empty) blocking signature set
    counts as no-progress. A completely new signature set, and any partial reduction of the
    previous set, both count as progress -- the Maker made *some* change even if it didn't
    fully resolve every finding. `evaluate_no_progress`'s callers must already have filtered
    to `lc.BLOCKING_SEVERITIES` (see `build_pr_iteration_findings`)."""
    prev = prw.IterationFindings(frozenset({"sig-a", "sig-b"}), 2)

    reraised = prw.evaluate_no_progress(
        prev, prw.IterationFindings(frozenset({"sig-a", "sig-b"}), 2)
    )
    new_signature_set = prw.evaluate_no_progress(
        prev, prw.IterationFindings(frozenset({"sig-a", "sig-c"}), 2)
    )
    partial_resolution = prw.evaluate_no_progress(
        prev, prw.IterationFindings(frozenset({"sig-a"}), 1)
    )
    fully_resolved = prw.evaluate_no_progress(prev, prw.IterationFindings(frozenset(), 0))

    assert reraised.no_progress is True
    assert reraised.reason == "reraised"
    assert reraised.reraised_signatures == frozenset({"sig-a", "sig-b"})
    assert new_signature_set.no_progress is False
    assert new_signature_set.reason == "progress"
    assert partial_resolution.no_progress is False
    assert partial_resolution.reason == "progress"
    assert fully_resolved.no_progress is False
    assert fully_resolved.reason == "progress"


def test_ev101_iteration_findings_severities_filter_excludes_medium_low() -> None:
    """issue #213/B (EV-101): the no-progress guard and phase signature consume
    `build_pr_iteration_findings(severities=lc.BLOCKING_SEVERITIES)`, so open medium/low
    findings must appear neither in the blocking signature set nor in `new_count` --
    their arrival or churn can never pollute the no-progress streak. Dismissed records
    stay excluded regardless of severity."""

    def record(severity: str, status: str, first_seen: int, last_seen: int) -> dict[str, object]:
        return {
            "severity": severity,
            "status": status,
            "first_seen_iteration": first_seen,
            "last_seen_iteration": last_seen,
        }

    pr_review = {
        "findings": {
            "sig-crit": record("critical", "open", 1, 2),
            "sig-high": record("high", "open", 2, 2),
            "sig-med": record("medium", "open", 2, 2),
            "sig-low": record("low", "open", 2, 2),
            "sig-dismissed-high": record("high", "dismissed", 2, 2),
        }
    }

    unfiltered = lc.build_pr_iteration_findings(pr_review, 2)
    blocking = lc.build_pr_iteration_findings(pr_review, 2, severities=lc.BLOCKING_SEVERITIES)

    assert unfiltered.signatures == frozenset({"sig-crit", "sig-high", "sig-med", "sig-low"})
    assert unfiltered.new_count == 3
    assert blocking.signatures == frozenset({"sig-crit", "sig-high"})
    assert blocking.new_count == 1
    # Identical blocking set across iterations stays no-progress even while lows churn.
    assert lc.evaluate_pr_review_no_progress(blocking, blocking).no_progress is True


def _pr_review_round(
    source_comment_id: str,
    previous_signatures: tuple[str, ...],
    current_signatures: tuple[str, ...],
) -> lc.PhaseCheckResult:
    """Build a `phase_check_from_review_findings()` result for one blocking-signature round."""
    return prw.phase_check_from_review_findings(
        prw.ReviewFindingsResult(
            findings=(
                prw.ImportedFinding("sig-a", "high", source_comment_id, "A", "app.py", 10, False),
            ),
            iteration_findings=lc.IterationFindings(
                frozenset(current_signatures), len(current_signatures)
            ),
            previous_iteration_findings=lc.IterationFindings(
                frozenset(previous_signatures), len(previous_signatures)
            ),
            open_non_blocking=(),
            processed_comment_ids=(source_comment_id,),
            ignored_untrusted_comment_count=0,
            needs_classification_count=0,
        )
    )


def test_ev40_loop_common_uses_pr_review_metadata_in_guard_path(tmp_path: Path) -> None:
    """issue #213/A: the guard routes through PR-review-specific metadata (not the phase's raw
    signature) and only treats an *exact* re-raise of the same blocking signature set as
    no-progress. Round 1 introduces `sig-a` (nothing preceded it: progress). Round 2 re-raises
    the identical `sig-a` set unchanged (no-progress, streak 1). Round 3 re-raises it again
    (streak 2 == `repeat`, EXIT_FAILURE)."""
    state = lc._initial_state(
        "loop", "issue-loop", "hash", str(tmp_path), "loop/issue-1", "pr_review_response"
    )
    phase_def = {
        "guards": {"max_iterations": 5, "no_progress": {"signature": "pr_review", "repeat": 2}},
        "on_failure": {"disposition": lc.Action.EXIT_FAILURE.value},
    }
    config = {"guards": {"max_iterations": 5, "no_progress": {"repeat": 2}}}

    round1 = _pr_review_round("review_comment:1", (), ("sig-a",))
    round2 = _pr_review_round("review_comment:2", ("sig-a",), ("sig-a",))
    round3 = _pr_review_round("review_comment:3", ("sig-a",), ("sig-a",))

    first_decision = lc.evaluate_guards(state, round1, phase_def, config)
    second_decision = lc.evaluate_guards(state, round2, phase_def, config)
    third_decision = lc.evaluate_guards(state, round3, phase_def, config)

    assert first_decision.disposition == "continue"
    assert second_decision.disposition == "continue"
    assert third_decision.disposition == lc.Action.EXIT_FAILURE.value
    assert third_decision.reason == "no_progress"

    state_without_phase_def = lc._initial_state(
        "loop-2", "issue-loop", "hash", str(tmp_path), "loop/issue-1", "pr_review_response"
    )
    lc.evaluate_guards(state_without_phase_def, round1, None, config)
    lc.evaluate_guards(state_without_phase_def, round2, None, config)
    fallback_decision = lc.evaluate_guards(state_without_phase_def, round3, None, config)
    assert fallback_decision.reason == "no_progress"


def test_confirm_review_findings_reported_blocks_on_concurrent_flock_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DH2: `_fenced_pr_review_write` must hold the lock-file's own flock across validation
    and the write, so a concurrent flock holder on the same path (e.g. `loop_status.py`
    purge, or another worker's lease reacquisition) is serialized against, not raced.
    Before the fix, `_fence_state_update` validated the lease/pending-action and returned
    without holding any flock across the caller's subsequent journal/state write."""
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
    result = prw.ReviewFindingsResult(
        findings=(
            prw.ImportedFinding(
                "sig-a", "high", "review_comment:1", "explicit blocker", None, None, False
            ),
        ),
        iteration_findings=lc.IterationFindings(frozenset({"sig-a"}), 1),
        previous_iteration_findings=lc.IterationFindings(frozenset(), 0),
        open_non_blocking=(),
        processed_comment_ids=(),
        ignored_untrusted_comment_count=0,
        needs_classification_count=0,
    )

    lock_file = lc.lock_path("abcd1234-issue-1", project_dir)
    held = lock_file.open("r+", encoding="utf-8")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)

    events: list[str] = []

    def _confirm() -> None:
        prw.confirm_review_findings_reported("abcd1234-issue-1", project_dir, result, lease_token)
        events.append("confirmed")

    thread = threading.Thread(target=_confirm)
    thread.start()
    try:
        time.sleep(0.2)
        # While this test still holds the flock, the confirm's write must be blocked, not
        # racing ahead to mutate state.json underneath it.
        assert events == []
        events.append("released")
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert events == ["released", "confirmed"]
    state = lc.load_state("abcd1234-issue-1", project_dir)
    assert "review_comment:1" in state.pr_review["processed_comment_ids"]


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


# --- issue #235: addressed findings (auto reply/resolve + exit-comment matrix) ------------


def _open_finding_record(source_comment_ids: list[str], **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "first_seen_iteration": 1,
        "last_seen_iteration": 1,
        "status": "open",
        "severity": "high",
        "dismiss_reason": None,
        "source_comment_ids": source_comment_ids,
        "path": "app.py",
        "line": 10,
    }
    record.update(overrides)
    return record


def test_mark_addressed_findings_marks_open_records_and_skips_dismissed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "processed_comment_ids": [],
            "findings": {
                "sig-open": _open_finding_record(["review_comment:1"]),
                "sig-dismissed": _open_finding_record(
                    ["review_comment:2"], status="dismissed", severity="medium"
                ),
            },
        },
    )
    lease_token = _lease(project_dir)

    addressed = prw.mark_addressed_findings(
        "abcd1234-issue-1",
        project_dir,
        ["sig-open", "sig-dismissed", "sig-missing"],
        "cafebabecafebabe",
        2,
        lease_token,
    )

    assert addressed == ("sig-open",)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    record = state.pr_review["findings"]["sig-open"]
    assert record["status"] == "addressed"
    assert record["addressed_at_commit"] == "cafebabecafebabe"
    assert record["addressed_at_iteration"] == 2
    assert state.pr_review["findings"]["sig-dismissed"]["status"] == "dismissed"


def test_mark_addressed_findings_no_candidates_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path, monkeypatch, pr_review={"processed_comment_ids": [], "findings": {}}
    )
    lease_token = _lease(project_dir)

    assert (
        prw.mark_addressed_findings("abcd1234-issue-1", project_dir, [], "sha", 1, lease_token)
        == ()
    )


def test_reraise_after_addressed_reopens_and_allows_later_re_addressing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full issue #235 reraise cycle: `mark_addressed_findings()` marks a signature "addressed"
    at commit A, the same signature reraises (re-imported via `_upsert_finding()`, e.g. the
    Maker's commit A did not actually fix it), and only *then* is it truly fixed at commit B.
    The reraise must (1) flip `status` back to "open" so the exit matrix stops calling it
    resolved, and (2) clear the stale commit-A `addressed_at_*` markers so
    `mark_addressed_findings()`'s `status == "open"` guard accepts the commit-B re-addressing
    instead of silently skipping it (the bug: without the reopen, the record stays pinned at
    commit A forever)."""
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "processed_comment_ids": [],
            "findings": {"sig-a": _open_finding_record(["review_comment:1"])},
        },
    )
    lease_token = _lease(project_dir)
    prw.mark_addressed_findings(
        "abcd1234-issue-1", project_dir, ["sig-a"], "commitaaaaaaaaaa", 2, lease_token
    )
    state = lc.load_state("abcd1234-issue-1", project_dir)
    assert state.pr_review["findings"]["sig-a"]["status"] == "addressed"

    # The signature reraises at iteration 3: re-imported via _upsert_finding, then persisted the
    # same way collect_review_findings() would.
    findings_map = state.pr_review["findings"]
    prw._upsert_finding(
        findings_map,
        prw.ImportedFinding("sig-a", "high", "review_comment:2", "[P1]", "app.py", 10, False),
        3,
    )
    state.pr_review["findings"] = findings_map
    lc._write_state(state, project_dir)

    reopened = lc.load_state("abcd1234-issue-1", project_dir)
    reopened_record = reopened.pr_review["findings"]["sig-a"]
    assert reopened_record["status"] == "open"
    assert reopened_record["addressed_at_commit"] is None
    assert reopened_record["addressed_at_iteration"] is None

    # Commit B genuinely fixes it: mark_addressed_findings() must accept the re-addressing
    # instead of the status == "open" guard skipping an already-"addressed" record. Reuses the
    # still-active lease (acquire_lock is single-holder; this loop_id's lease was never
    # released, so a second acquire_lock() call here would deadlock/fail).
    addressed_again = prw.mark_addressed_findings(
        "abcd1234-issue-1", project_dir, ["sig-a"], "commitbbbbbbbbbb", 4, lease_token
    )

    assert addressed_again == ("sig-a",)
    final_state = lc.load_state("abcd1234-issue-1", project_dir)
    final_record = final_state.pr_review["findings"]["sig-a"]
    assert final_record["status"] == "addressed"
    assert final_record["addressed_at_commit"] == "commitbbbbbbbbbb"
    assert final_record["addressed_at_iteration"] == 4


class _FakeGitWorkflowModule:
    """Fake `pr_review_threads` module for `resolve_addressed_findings` tests."""

    class GhCommandError(RuntimeError):
        pass

    def __init__(
        self,
        fetch_result: dict[str, Any],
        *,
        fail_reply_for: tuple[int, ...] = (),
        fail_resolve_for: tuple[str, ...] = (),
        fetch_error: Exception | None = None,
    ) -> None:
        self._fetch_result = fetch_result
        self._fail_reply_for = set(fail_reply_for)
        self._fail_resolve_for = set(fail_resolve_for)
        self._fetch_error = fetch_error
        self.reply_calls: list[tuple[Any, ...]] = []
        self.resolve_calls: list[str] = []

    def fetch_review_threads(
        self, _pr_number: int, _project_dir: str, _timeout: int
    ) -> dict[str, Any]:
        if self._fetch_error is not None:
            raise self._fetch_error
        return self._fetch_result

    def reply_to_comment(
        self,
        owner: str,
        name: str,
        pr_number: int,
        comment_id: int,
        body_file: Path,
        *,
        issue_comment: bool,
        timeout: int,
    ) -> dict[str, Any]:
        self.reply_calls.append((owner, name, pr_number, comment_id, body_file.read_text()))
        if comment_id in self._fail_reply_for:
            raise self.GhCommandError("reply failed")
        return {"status": "ok", "comment_id": 999}

    def resolve_thread(self, thread_id: str, _timeout: int) -> dict[str, Any]:
        self.resolve_calls.append(thread_id)
        if thread_id in self._fail_resolve_for:
            raise self.GhCommandError("resolve failed")
        return {"thread_id": thread_id, "is_resolved": True}


def _trusted_thread(thread_id: str, comment_id: int) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "is_outdated": False,
        "path": "app.py",
        "line": 10,
        "has_non_bot_comments": False,
        "comments": [{"comment_id": comment_id, "reply_target_id": comment_id}],
    }


def test_resolve_addressed_findings_replies_and_resolves_trusted_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "processed_comment_ids": [],
            "findings": {
                "sig-addressed": _open_finding_record(
                    ["review_comment:1"],
                    status="addressed",
                    severity="critical",
                    addressed_at_commit="cafebabecafebabe",
                    addressed_at_iteration=2,
                ),
            },
        },
    )
    lease_token = _lease(project_dir)
    fake = _FakeGitWorkflowModule({"unresolved_threads": [_trusted_thread("THREAD-1", 1)]})
    monkeypatch.setattr(prw, "_load_git_workflow_module", lambda: fake)

    result = prw.resolve_addressed_findings(
        "abcd1234-issue-1",
        project_dir,
        12,
        "owner/repo",
        ["sig-addressed"],
        "cafebabecafebabe",
        lease_token,
    )

    assert result.resolved_signatures == ("sig-addressed",)
    assert result.git_workflow_unavailable is False
    assert len(result.thread_outcomes) == 1
    outcome = result.thread_outcomes[0]
    assert outcome.status == "resolved"
    assert outcome.thread_id == "THREAD-1"
    assert outcome.comment_id == 1
    assert fake.resolve_calls == ["THREAD-1"]
    assert fake.reply_calls[0][:4] == ("owner", "repo", 12, 1)
    assert "cafebab" in fake.reply_calls[0][4]

    state = lc.load_state("abcd1234-issue-1", project_dir)
    assert state.pr_review["findings"]["sig-addressed"]["resolved_thread_ids"] == ["THREAD-1"]


def test_resolve_addressed_findings_skips_already_resolved_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "processed_comment_ids": [],
            "findings": {
                "sig-addressed": _open_finding_record(
                    ["review_comment:1"],
                    status="addressed",
                    resolved_thread_ids=["THREAD-1"],
                ),
            },
        },
    )
    lease_token = _lease(project_dir)
    fake = _FakeGitWorkflowModule({"unresolved_threads": [_trusted_thread("THREAD-1", 1)]})
    monkeypatch.setattr(prw, "_load_git_workflow_module", lambda: fake)

    result = prw.resolve_addressed_findings(
        "abcd1234-issue-1", project_dir, 12, "owner/repo", ["sig-addressed"], "sha", lease_token
    )

    assert result.resolved_signatures == ()
    assert result.thread_outcomes[0].status == "already_resolved"
    assert fake.reply_calls == []
    assert fake.resolve_calls == []


def test_resolve_addressed_findings_reports_no_review_comment_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "processed_comment_ids": [],
            "findings": {
                "sig-issue-only": _open_finding_record(["issue_comment:9"], status="addressed"),
            },
        },
    )
    lease_token = _lease(project_dir)
    fake = _FakeGitWorkflowModule({"unresolved_threads": []})
    monkeypatch.setattr(prw, "_load_git_workflow_module", lambda: fake)

    result = prw.resolve_addressed_findings(
        "abcd1234-issue-1", project_dir, 12, "owner/repo", ["sig-issue-only"], "sha", lease_token
    )

    assert result.thread_outcomes[0].status == "no_review_comment_source"
    assert result.resolved_signatures == ()


def test_resolve_addressed_findings_reports_no_trusted_thread_for_mixed_bot_human_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thread with any non-bot comment (`has_non_bot_comments`) is never touched (issue
    #235's safety boundary): the finding's own comment is dropped from the trusted index."""
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "processed_comment_ids": [],
            "findings": {
                "sig-addressed": _open_finding_record(["review_comment:1"], status="addressed"),
            },
        },
    )
    lease_token = _lease(project_dir)
    mixed_thread = _trusted_thread("THREAD-1", 1)
    mixed_thread["has_non_bot_comments"] = True
    fake = _FakeGitWorkflowModule({"unresolved_threads": [mixed_thread]})
    monkeypatch.setattr(prw, "_load_git_workflow_module", lambda: fake)

    result = prw.resolve_addressed_findings(
        "abcd1234-issue-1", project_dir, 12, "owner/repo", ["sig-addressed"], "sha", lease_token
    )

    assert result.thread_outcomes[0].status == "no_trusted_thread"
    assert fake.reply_calls == []
    assert fake.resolve_calls == []


def test_resolve_addressed_findings_reports_reply_and_resolve_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "processed_comment_ids": [],
            "findings": {
                "sig-reply-fails": _open_finding_record(["review_comment:1"], status="addressed"),
                "sig-resolve-fails": _open_finding_record(["review_comment:2"], status="addressed"),
            },
        },
    )
    lease_token = _lease(project_dir)
    fake = _FakeGitWorkflowModule(
        {
            "unresolved_threads": [
                _trusted_thread("THREAD-1", 1),
                _trusted_thread("THREAD-2", 2),
            ]
        },
        fail_reply_for=(1,),
        fail_resolve_for=("THREAD-2",),
    )
    monkeypatch.setattr(prw, "_load_git_workflow_module", lambda: fake)

    result = prw.resolve_addressed_findings(
        "abcd1234-issue-1",
        project_dir,
        12,
        "owner/repo",
        ["sig-reply-fails", "sig-resolve-fails"],
        "sha",
        lease_token,
    )

    outcomes = {outcome.signature: outcome for outcome in result.thread_outcomes}
    assert outcomes["sig-reply-fails"].status == "reply_failed"
    assert outcomes["sig-resolve-fails"].status == "resolve_failed"
    assert result.resolved_signatures == ()

    state = lc.load_state("abcd1234-issue-1", project_dir)
    findings = state.pr_review["findings"]
    assert "resolved_thread_ids" not in findings["sig-reply-fails"]
    assert "resolved_thread_ids" not in findings["sig-resolve-fails"]


def test_resolve_addressed_findings_returns_unavailable_when_git_workflow_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path, monkeypatch, pr_review={"processed_comment_ids": [], "findings": {}}
    )
    lease_token = _lease(project_dir)
    monkeypatch.setattr(prw, "_load_git_workflow_module", lambda: None)

    result = prw.resolve_addressed_findings(
        "abcd1234-issue-1", project_dir, 12, "owner/repo", ["sig"], "sha", lease_token
    )

    assert result.git_workflow_unavailable is True
    assert result.resolved_signatures == ()
    assert result.thread_outcomes == ()


def test_resolve_addressed_findings_no_candidates_never_imports_git_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path, monkeypatch, pr_review={"processed_comment_ids": [], "findings": {}}
    )
    lease_token = _lease(project_dir)
    called = False

    def _fail_if_called() -> Any:
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(prw, "_load_git_workflow_module", _fail_if_called)

    result = prw.resolve_addressed_findings(
        "abcd1234-issue-1", project_dir, 12, "owner/repo", [], "sha", lease_token
    )

    assert called is False
    assert result == prw.AddressedFindingsResult((), (), git_workflow_unavailable=False)


def test_resolve_addressed_findings_survives_fetch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _setup_state(
        tmp_path,
        monkeypatch,
        pr_review={
            "processed_comment_ids": [],
            "findings": {
                "sig-addressed": _open_finding_record(["review_comment:1"], status="addressed"),
            },
        },
    )
    lease_token = _lease(project_dir)
    fake = _FakeGitWorkflowModule({}, fetch_error=RuntimeError("network down"))
    monkeypatch.setattr(prw, "_load_git_workflow_module", lambda: fake)

    result = prw.resolve_addressed_findings(
        "abcd1234-issue-1", project_dir, 12, "owner/repo", ["sig-addressed"], "sha", lease_token
    )

    assert result.git_workflow_unavailable is False
    assert result.resolved_signatures == ()
    assert result.thread_outcomes == ()
