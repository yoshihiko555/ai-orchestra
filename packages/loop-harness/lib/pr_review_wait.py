#!/usr/bin/env python3
"""Deterministic PR review wait/import helpers for loop-harness."""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import loop_common as lc  # noqa: E402
import loop_definition as ld  # noqa: E402

DEFAULT_POLL_INTERVAL_SECONDS = 120
DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_GH_API_TIMEOUT_SECONDS = 30
DEFAULT_GH_API_MAX_RETRIES = 3
DEFAULT_GH_API_BACKOFF_SECONDS = 1.0
DEFAULT_LINE_BUCKET_SIZE = 5
EXCERPT_LIMIT = 240
NON_BLOCKING_EXCERPT_LIMIT = 200
REVIEW_FINDINGS_SNAPSHOT_ARTIFACT = "review_findings.json"
# v2 (#213) added `open_non_blocking` to the snapshot payload.
REVIEW_FINDINGS_SNAPSHOT_SCHEMA_VERSION = 2
MAX_REVIEW_FINDINGS_SNAPSHOT_BYTES = 1024 * 1024
REVIEW_SOURCES = frozenset({"review", "review_comment", "issue_comment"})
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
SEVERITIES = frozenset(SEVERITY_ORDER)
Severity = Literal["critical", "high", "medium", "low"]
POSITIVE_REVIEW_SUMMARIES = frozenset(
    {
        "all good",
        "approved",
        "lgtm",
        "looks good",
        "looks good to me",
        "no issues",
        "no issues found",
        "nothing to fix",
    }
)
BASELINE_ACTIONS = frozenset({lc.Action.ADVANCE_PHASE.value, lc.Action.WAIT_EXTERNAL_REVIEW.value})
COLLECT_ACTIONS = frozenset({lc.Action.WAIT_EXTERNAL_REVIEW.value})
DISMISS_ACTIONS = frozenset({lc.Action.RUN_MAKER.value})
DEFAULT_STOPWORDS_EN = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "be",
        "for",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "please",
        "should",
        "the",
        "this",
        "to",
    }
)
DEFAULT_STOPWORDS_JA = frozenset({"が", "です", "で", "と", "に", "の", "は", "ます", "を"})
DEFAULT_FOOTER_PATTERNS = (r"(?ms)^---\s*$.*\Z",)
DEFAULT_AUTO_GENERATED_MARKERS: tuple[str, ...] = (
    "<!-- This is an auto-generated comment: summarize by coderabbit.ai",
    "<!-- This is an auto-generated comment: rate limited by coderabbit.ai",
    "<!-- This is an auto-generated comment: review in progress by coderabbit.ai",
    "<!-- This is an auto-generated reply by CodeRabbit",
)
TERMINAL_VERDICT_PATTERNS: tuple[str, ...] = (
    "didn't find any major issues",
    "no major issues",
    "didn't find any issues",
    "review complete",
    "lgtm",
    "looks good to me",
    "approved",
)
REVIEWER_UNAVAILABLE_REASON = "rate_limited"
RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "more reviews will be available in",
    "rate limit",
    "rate-limit",
    "rate limited",
    "rate-limited",
    "review quota exceeded",
)


class PrReviewWaitError(RuntimeError):
    """Base error for PR review wait failures."""


class ConfigError(PrReviewWaitError, ValueError):
    """Raised when pr_review config is unsafe or invalid."""


class GitHubApiError(PrReviewWaitError):
    """Raised when a GitHub API call fails."""


class DismissalError(PrReviewWaitError, ValueError):
    """Raised when a finding cannot be dismissed safely."""


@dataclass(frozen=True)
class ReviewerAllowlistEntry:
    """One trusted automated reviewer identity."""

    app_slug: str | None = None
    login: str | None = None
    user_type: str = "Bot"
    author_associations: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class DedupConfig:
    """Config for finding signature normalization."""

    line_bucket_size: int = DEFAULT_LINE_BUCKET_SIZE
    stopwords_en: frozenset[str] = field(default_factory=lambda: DEFAULT_STOPWORDS_EN)
    stopwords_ja: frozenset[str] = field(default_factory=lambda: DEFAULT_STOPWORDS_JA)
    signature_footer_patterns: tuple[str, ...] = DEFAULT_FOOTER_PATTERNS


@dataclass(frozen=True)
class PrReviewConfig:
    """Validated pr_review config."""

    reviewer_allowlist: tuple[ReviewerAllowlistEntry, ...]
    checkrun_allowlist: frozenset[str] = field(default_factory=frozenset)
    severity_markers: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    dedup: DedupConfig = field(default_factory=DedupConfig)
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    auto_generated_markers: tuple[str, ...] = DEFAULT_AUTO_GENERATED_MARKERS


@dataclass(frozen=True)
class ReviewItem:
    """Normalized item from GitHub review/comment APIs."""

    source: Literal["review", "review_comment", "issue_comment"]
    item_id: str
    body: str
    created_at: str | None
    path: str | None
    line: int | None
    original_line: int | None
    pull_request_review_id: int | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class BaselineRecord:
    """Recorded baseline before push or PR creation."""

    baseline_review_id: int
    baseline_recorded_at: str
    processed_comment_ids: tuple[str, ...]


@dataclass(frozen=True)
class PrReviewPushDelta:
    """Read-only comparison result for PR review response pushes."""

    status: str
    local_head_sha: str | None
    iteration_head_sha: str | None


@dataclass(frozen=True)
class IgnoredUntrustedReview:
    """Submitted review ignored because it does not match the reviewer allowlist."""

    review_id: int
    login: str | None
    author_association: str | None
    submitted_at: str | None
    body_excerpt: str


@dataclass(frozen=True)
class CompletionOutcome:
    """Result of polling for external review completion."""

    signal: Literal[
        "review_submitted",
        "issue_comment_completed",
        "check_run_completed",
        "reviewer_unavailable",
        "timeout",
        "api_error",
        "pending",
    ]
    completed: bool
    timed_out: bool
    infrastructure_failure: bool
    review_ids: tuple[int, ...] = ()
    check_run_names: tuple[str, ...] = ()
    issue_comment_ids: tuple[str, ...] = ()
    reviewer_unavailable_comment_ids: tuple[str, ...] = ()
    reviewer_unavailable_reason: str | None = None
    ignored_untrusted_review_count: int = 0
    ignored_untrusted_reviews: tuple[IgnoredUntrustedReview, ...] = ()
    error: str | None = None
    shortcut_reason: str | None = None
    local_head_sha: str | None = None
    iteration_head_sha: str | None = None


@dataclass(frozen=True)
class SeverityDecision:
    """Deterministic severity classification result."""

    severity: Severity | None
    source: Literal["explicit", "external_classification", "fail_safe"]
    needs_classification: bool
    reason: str


@dataclass(frozen=True)
class ImportedFinding:
    """Trusted imported PR review finding."""

    signature: str
    severity: Severity
    source_comment_id: str
    body_excerpt: str
    path: str | None
    line: int | None
    needs_classification: bool


IterationFindings = lc.IterationFindings
NoProgressResult = lc.NoProgressResult


@dataclass(frozen=True)
class NonBlockingFinding:
    """Currently open (non-dismissed), non-blocking (medium/low) PR review finding.

    Reported to Maker-facing exit surfaces (issue #213) so a `passed=true` exit from
    `pr_review_response` still surfaces low/medium findings nobody explicitly dismissed.
    """

    signature: str
    severity: Severity
    path: str | None
    line: int | None
    body_excerpt: str


@dataclass(frozen=True)
class ReviewFindingsResult:
    """Result of importing trusted review findings."""

    findings: tuple[ImportedFinding, ...]
    iteration_findings: IterationFindings
    previous_iteration_findings: IterationFindings
    open_non_blocking: tuple[NonBlockingFinding, ...]
    processed_comment_ids: tuple[str, ...]
    ignored_untrusted_comment_count: int
    needs_classification_count: int


@dataclass(frozen=True)
class AddressedThreadOutcome:
    """One trusted GitHub review thread's reply/resolve outcome for an addressed finding.

    Issue #235: `resolve_addressed_findings` reports one or more of these per candidate
    signature (PR #276 review: one per distinct trusted thread its accumulated
    `review_comment:` ids map to, not just the latest) so a best-effort GitHub side-effect
    failure (rate limit, transient API error, thread already resolved by a human, ...) is
    fully observable without ever raising out of that function.

    PR #276 review (P2, round 2): `"lease_expired"` covers a signature/thread this call never
    even attempted to reply to/resolve on GitHub because the caller-held lease was found
    invalid (expired or reacquired by another worker) immediately before that GitHub write --
    see `resolve_addressed_findings`'s own docstring for why this guard exists in addition to
    `_fenced_pr_review_write`'s state-write-only fencing.
    """

    signature: str
    thread_id: str | None
    comment_id: int | None
    status: Literal[
        "resolved",
        "no_review_comment_source",
        "no_trusted_thread",
        "already_resolved",
        "reply_failed",
        "resolve_failed",
        "lease_expired",
    ]
    error: str | None = None


@dataclass(frozen=True)
class AddressedFindingsResult:
    """Result of best-effort resolving GitHub review threads for addressed findings."""

    resolved_signatures: tuple[str, ...]
    thread_outcomes: tuple[AddressedThreadOutcome, ...]
    git_workflow_unavailable: bool


@dataclass(frozen=True)
class AppliedSeverityClassification:
    """One classification persisted to PR review state."""

    signature: str
    source_comment_id: str
    severity: Severity | None
    source: Literal["explicit", "external_classification", "fail_safe"]
    reason: str


@dataclass(frozen=True)
class ClassificationApplicationResult:
    """State-backed result of applying external severity classifications."""

    review_findings: ReviewFindingsResult
    classifications: tuple[AppliedSeverityClassification, ...]


class GhApiClient:
    """Small `gh api` wrapper with bounded subprocess timeouts."""

    def __init__(
        self,
        repo: str,
        timeout_seconds: int = DEFAULT_GH_API_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_GH_API_MAX_RETRIES,
        backoff_base_seconds: float = DEFAULT_GH_API_BACKOFF_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repo = repo
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(1, max_retries)
        self.backoff_base_seconds = backoff_base_seconds
        self.sleeper = sleeper

    def api(self, path: str) -> Any:
        """Call `gh api` and parse JSON output."""
        output = self._api_stdout(path)
        try:
            return _loads_paginated_json(output)
        except json.JSONDecodeError as exc:
            raise GitHubApiError(f"invalid gh api JSON: {path}") from exc

    def _api_stdout(self, path: str) -> str:
        """Call `gh api --paginate` with bounded retries for transient failures."""
        last_error: GitHubApiError | None = None
        for attempt in range(self.max_retries):
            try:
                return self._api_stdout_once(path)
            except GitHubApiError as exc:
                last_error = exc
                if attempt + 1 >= self.max_retries:
                    break
                self.sleeper(self.backoff_base_seconds * (2**attempt))
        assert last_error is not None
        raise last_error

    def _api_stdout_once(self, path: str) -> str:
        """Call `gh api --paginate` once and return stdout."""
        try:
            proc = subprocess.run(
                ["gh", "api", "--paginate", path],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitHubApiError(str(exc)) from exc
        if proc.returncode != 0:
            raise GitHubApiError((proc.stderr or proc.stdout or "gh api failed").strip())
        return proc.stdout or "null"


def load_pr_review_config(project_dir: str) -> PrReviewConfig:
    """Load and validate layered loop-harness pr_review config."""
    raw = ld.load_config(project_dir)
    return parse_pr_review_config(raw)


def parse_pr_review_config(config: dict[str, Any]) -> PrReviewConfig:
    """Validate pr_review config and fail closed without a reviewer allowlist."""
    pr_review = config.get("pr_review") if isinstance(config.get("pr_review"), dict) else {}
    allowlist = _parse_reviewer_allowlist(pr_review.get("reviewer_allowlist"))
    checkruns = _string_set(pr_review.get("checkrun_allowlist"))
    return PrReviewConfig(
        reviewer_allowlist=allowlist,
        checkrun_allowlist=checkruns,
        severity_markers=_parse_severity_markers(pr_review.get("severity_markers")),
        dedup=_parse_dedup_config(pr_review.get("dedup")),
        poll_interval_seconds=_positive_int(
            pr_review.get("poll_interval_seconds"), DEFAULT_POLL_INTERVAL_SECONDS
        ),
        timeout_seconds=_positive_int(pr_review.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS),
        auto_generated_markers=_parse_auto_generated_markers(
            pr_review.get("auto_generated_markers")
        ),
    )


def record_baseline(
    loop_id: str,
    project_dir: str,
    pr_number: int | None,
    client: GhApiClient,
    lease_token: str,
    *,
    action_id: str | None = None,
    review_items: list[ReviewItem] | None = None,
    snapshot_captured_at: str | None = None,
) -> BaselineRecord:
    """Record review/comment baseline before push or PR creation.

    `review_items` is an optional pre-fetched snapshot (see `fetch_review_items`). Passing
    the same snapshot a caller already fetched for `collect_review_findings` avoids a second,
    later `gh api` round trip here (DC3): drain-then-rebaseline previously performed two
    *separate* fetches, leaving a window where a comment posted between them would be
    silently marked `processed` by this function's own (later) fetch without ever being
    imported as a finding by the drain's (earlier) fetch, permanently losing it. When
    `review_items` is omitted, this fetches fresh itself (unchanged, backward-compatible
    behavior for callers that do not need to share a snapshot, e.g. PR-creation baselining).

    code L3: `snapshot_captured_at` lets a caller reusing a pre-fetched `review_items` snapshot
    also pass through *when that snapshot was fetched*, instead of this function stamping
    `baseline_recorded_at` with "now". Without this, a caller like `_drain_before_push()` that
    fetches `review_items` once, spends real time on severity classification (an LLM call per
    finding needing it), and only *then* calls this function with the same stale snapshot would
    get a `baseline_recorded_at` timestamped *after* that classification delay. A review/comment
    posted after the snapshot but before that later write has a `created_at` older than the new
    baseline, so `_is_importable()`'s `created_at > baseline_recorded_at` check would filter it
    out forever -- the finding is silently and permanently lost, exactly the drain-gap failure
    mode this function's baseline is meant to close. Passing the snapshot's own fetch time keeps
    the baseline honest about what it actually reflects. Ignored (falls back to `lc.now_iso()`,
    the original behavior) when `review_items` is also omitted, since there is then no shared
    snapshot whose fetch time would matter here.
    """
    recorded_at = snapshot_captured_at if snapshot_captured_at is not None else lc.now_iso()
    if pr_number is None or pr_number <= 0:
        baseline_review_id = 0
        processed_ids: set[str] = set()
    else:
        items = review_items if review_items is not None else fetch_review_items(client, pr_number)
        baseline_review_id = max(
            [_int_or_zero(item.raw.get("id")) for item in items if item.source == "review"] or [0]
        )
        processed_ids = {_comment_key(item) for item in items}
    state = lc.load_state(loop_id, project_dir)
    pr_review = _ensure_pr_review_state(state.pr_review)
    existing = set(_processed_comment_ids(pr_review))
    pr_review["baseline_review_id"] = baseline_review_id
    pr_review["baseline_recorded_at"] = recorded_at
    pr_review["processed_comment_ids"] = sorted(existing | processed_ids)
    state.pr_review = pr_review
    state.updated_at = lc.now_iso()
    with _fenced_pr_review_write(
        state, loop_id, project_dir, lease_token, action_id, BASELINE_ACTIONS
    ):
        lc.append_journal_event(
            loop_id,
            project_dir,
            "pr_review_baseline_recorded",
            "waiter",
            action_id,
            {
                "baseline_review_id": baseline_review_id,
                "baseline_recorded_at": recorded_at,
                "processed_comment_count": len(pr_review["processed_comment_ids"]),
            },
        )
        lc._write_state(state, project_dir)
    return BaselineRecord(
        baseline_review_id,
        recorded_at,
        tuple(pr_review["processed_comment_ids"]),
    )


def record_iteration_head(
    loop_id: str,
    project_dir: str,
    pr_number: int,
    client: GhApiClient,
    lease_token: str,
    *,
    action_id: str | None = None,
    iteration: int | None = None,
) -> str:
    """Record the post-push PR head SHA for check-run fallback scoping.

    `iteration` (DH5), when given, is durably recorded alongside `iteration_head_sha` as
    `iteration_head_recorded_iteration`. This lets a resumed `wait_external_review` (e.g.
    after a driver crash between this call succeeding and its poll actually starting)
    distinguish "this push already happened for the *current* iteration, just go poll" from
    the unrelated, genuinely-nothing-to-push case where `iteration_head_sha` is merely stale
    from an earlier iteration (see `loop_driver._already_pushed_this_iteration`).
    """
    payload = client.api(f"repos/{client.repo}/pulls/{pr_number}")
    head = payload.get("head") if isinstance(payload, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not sha:
        raise GitHubApiError("pull request head.sha is missing")
    state = lc.load_state(loop_id, project_dir)
    pr_review = _ensure_pr_review_state(state.pr_review)
    pr_review["iteration_head_sha"] = sha
    if iteration is not None:
        pr_review["iteration_head_recorded_iteration"] = iteration
    state.pr_review = pr_review
    state.updated_at = lc.now_iso()
    with _fenced_pr_review_write(
        state, loop_id, project_dir, lease_token, action_id, BASELINE_ACTIONS
    ):
        lc.append_journal_event(
            loop_id,
            project_dir,
            "pr_review_iteration_head_recorded",
            "waiter",
            action_id,
            {"iteration_head_sha": sha},
        )
        lc._write_state(state, project_dir)
    return sha


def detect_pr_review_push_delta(
    loop_id: str, project_dir: str, worktree_path: str
) -> PrReviewPushDelta:
    """Read-only comparison of worktree HEAD vs the last recorded PR iteration head sha."""
    local_head_sha = _local_head_sha(worktree_path)
    state = lc.load_state(loop_id, project_dir)
    pr_review = state.pr_review if isinstance(state.pr_review, dict) else {}
    iteration_head_sha = _optional_str(pr_review.get("iteration_head_sha"))
    if local_head_sha is None or iteration_head_sha is None:
        return PrReviewPushDelta("unknown", local_head_sha, iteration_head_sha)
    if local_head_sha == iteration_head_sha:
        return PrReviewPushDelta("no_new_commit", local_head_sha, iteration_head_sha)
    return PrReviewPushDelta("new_commit", local_head_sha, iteration_head_sha)


def no_new_commit_completion_outcome(delta: PrReviewPushDelta) -> CompletionOutcome:
    """Build a timeout-equivalent CompletionOutcome for the no_new_commit shortcut."""
    if delta.status != "no_new_commit":
        raise PrReviewWaitError("no_new_commit_completion_outcome requires status=no_new_commit")
    return CompletionOutcome(
        "timeout",
        completed=False,
        timed_out=True,
        infrastructure_failure=False,
        shortcut_reason="no_new_commit_to_push",
        local_head_sha=delta.local_head_sha,
        iteration_head_sha=delta.iteration_head_sha,
    )


def wait_for_completion(
    pr_number: int,
    baseline: dict[str, Any],
    config: PrReviewConfig,
    client: GhApiClient,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    heartbeat: Callable[[], None] | None = None,
) -> CompletionOutcome:
    """Poll until a trusted review or configured check-run completion appears."""
    start = monotonic()
    ignored_reviews: dict[str, IgnoredUntrustedReview] = {}
    reviewer_unavailable_outcome: CompletionOutcome | None = None
    while True:
        try:
            review_outcome = _review_completion_outcome(
                pr_number, baseline, config, client, ignored_reviews
            )
            if review_outcome.completed:
                return review_outcome
            issue_comment_outcome = _issue_comment_completion_outcome(
                pr_number, baseline, config, client
            )
            if issue_comment_outcome.completed:
                return _completion_outcome_with_ignored_reviews(
                    issue_comment_outcome, ignored_reviews
                )
            check_outcome = _checkrun_completion_outcome(baseline, config, client)
            if check_outcome.completed:
                return _completion_outcome_with_ignored_reviews(check_outcome, ignored_reviews)
            observed_unavailable = _reviewer_unavailable_outcome(
                pr_number, baseline, config, client
            )
            if observed_unavailable is not None:
                reviewer_unavailable_outcome = observed_unavailable
                if not _has_alternate_review_path(config):
                    return _completion_outcome_with_ignored_reviews(
                        reviewer_unavailable_outcome, ignored_reviews
                    )
        except GitHubApiError as exc:
            return CompletionOutcome(
                "api_error",
                completed=False,
                timed_out=False,
                infrastructure_failure=True,
                ignored_untrusted_review_count=len(ignored_reviews),
                ignored_untrusted_reviews=_ignored_review_tuple(ignored_reviews),
                error=str(exc),
            )
        if monotonic() - start >= config.timeout_seconds:
            if reviewer_unavailable_outcome is not None:
                return _completion_outcome_with_ignored_reviews(
                    reviewer_unavailable_outcome, ignored_reviews
                )
            return CompletionOutcome(
                "timeout",
                completed=False,
                timed_out=True,
                infrastructure_failure=False,
                ignored_untrusted_review_count=len(ignored_reviews),
                ignored_untrusted_reviews=_ignored_review_tuple(ignored_reviews),
            )
        if heartbeat is not None:
            heartbeat()
        sleeper(config.poll_interval_seconds)


def collect_review_findings(
    loop_id: str,
    project_dir: str,
    pr_number: int,
    config: PrReviewConfig,
    client: GhApiClient,
    iteration: int,
    lease_token: str,
    *,
    action_id: str | None = None,
    review_items: list[ReviewItem] | None = None,
) -> ReviewFindingsResult:
    """Import trusted post-baseline review findings and update loop state.

    `review_items` is an optional pre-fetched snapshot (see `fetch_review_items`, DC3); when
    omitted this fetches fresh itself (unchanged, backward-compatible default).

    A finding is *not* marked `processed` here even when it has an explicit severity (i.e.
    does not need classification) (DC4). Marking it processed in this same write, before the
    caller has durably reflected `result.findings` anywhere, means a crash between this
    write and the caller's reflection would leave the comment permanently `processed` but
    the finding itself never actually reported: a retried `collect_review_findings` call
    would then treat the comment as already-handled and silently omit it from the returned
    `findings`, even though nothing ever consumed it. Deferring `processed` marking to an
    explicit caller confirmation (`confirm_review_findings_reported`, called only after the
    caller has durably captured the result, e.g. via `save_review_findings_snapshot`) mirrors
    the same defer-until-reflected discipline `apply_severity_classifications` already uses
    for findings that need classification.
    """
    review_items = (
        review_items if review_items is not None else fetch_review_items(client, pr_number)
    )
    state = lc.load_state(loop_id, project_dir)
    pr_review = _ensure_pr_review_state(state.pr_review)
    baseline = _baseline_from_state(pr_review)
    processed = set(_processed_comment_ids(pr_review))
    findings_map = _findings_map(pr_review)
    # Blocking-only (critical/high) on purpose: this feeds the no-progress guard and the phase
    # signature via `phase_check_from_review_findings`, and low/medium churn must never affect
    # either (issue #213). The all-severity summary used for journal observability is computed
    # separately below, after `findings_map` reflects this action's imports.
    previous_iteration_findings = lc.build_pr_iteration_findings(
        pr_review, iteration - 1, severities=lc.BLOCKING_SEVERITIES
    )
    imported: list[ImportedFinding] = []
    ignored_items: list[ReviewItem] = []

    for item in review_items:
        key = _comment_key(item)
        if not _is_importable(item, baseline, processed):
            continue
        if not verify_origin(item.raw, config.reviewer_allowlist):
            processed.add(key)
            ignored_items.append(item)
            continue
        finding = _finding_from_item(item, key, config, iteration)
        if finding is None:
            processed.add(key)
            continue
        imported.append(finding)
        _upsert_finding(findings_map, finding, iteration)

    pr_review["processed_comment_ids"] = sorted(processed)
    pr_review["findings"] = findings_map
    state.pr_review = pr_review
    state.ignored_untrusted_comment_count += len(ignored_items)
    state.updated_at = lc.now_iso()
    # All-severity summary, unchanged from prior behavior: kept for journal observability only
    # (not fed into the no-progress guard).
    all_severity_iteration_findings = build_iteration_findings(pr_review, iteration)
    iteration_findings = lc.build_pr_iteration_findings(
        pr_review, iteration, severities=lc.BLOCKING_SEVERITIES
    )
    open_non_blocking = _open_non_blocking_findings(findings_map)
    with _fenced_pr_review_write(
        state, loop_id, project_dir, lease_token, action_id, COLLECT_ACTIONS
    ):
        for item in ignored_items:
            _journal_ignored_untrusted(loop_id, project_dir, action_id, item)
        lc.append_journal_event(
            loop_id,
            project_dir,
            "pr_review_findings_imported",
            "waiter",
            action_id,
            {
                "imported_count": len(imported),
                "ignored_untrusted_comment_count": len(ignored_items),
                "signatures": sorted(all_severity_iteration_findings.signatures),
                "new_count": all_severity_iteration_findings.new_count,
            },
        )
        lc._write_state(state, project_dir)
    return ReviewFindingsResult(
        findings=tuple(imported),
        iteration_findings=iteration_findings,
        previous_iteration_findings=previous_iteration_findings,
        open_non_blocking=open_non_blocking,
        processed_comment_ids=tuple(pr_review["processed_comment_ids"]),
        ignored_untrusted_comment_count=len(ignored_items),
        needs_classification_count=sum(1 for item in imported if item.needs_classification),
    )


def confirm_review_findings_reported(
    loop_id: str,
    project_dir: str,
    result: ReviewFindingsResult,
    lease_token: str,
    *,
    action_id: str | None = None,
) -> None:
    """Mark explicit-severity (non-classification-pending) findings processed (DC4).

    Call this only after the caller has durably captured `result` (e.g. after
    `save_review_findings_snapshot` succeeds). `collect_review_findings` intentionally
    leaves these comments out of `processed_comment_ids` so a crash before this call causes
    the next `collect_review_findings` to safely re-import and re-report them instead of
    silently dropping them. No-op when there is nothing to confirm.
    """
    confirmable = {
        finding.source_comment_id for finding in result.findings if not finding.needs_classification
    }
    if not confirmable:
        return
    state = lc.load_state(loop_id, project_dir)
    pr_review = _ensure_pr_review_state(state.pr_review)
    processed = set(_processed_comment_ids(pr_review))
    processed |= confirmable
    pr_review["processed_comment_ids"] = sorted(processed)
    state.pr_review = pr_review
    state.updated_at = lc.now_iso()
    with _fenced_pr_review_write(
        state, loop_id, project_dir, lease_token, action_id, COLLECT_ACTIONS
    ):
        lc._write_state(state, project_dir)


def save_review_findings_snapshot(
    loop_id: str,
    project_dir: str,
    action_id: str,
    result: ReviewFindingsResult,
    lease_token: str,
) -> str:
    """Persist one action's complete review findings result for later processes."""
    _validate_review_findings_snapshot_action(loop_id, project_dir, action_id, lease_token)
    payload = lc.redact_payload(
        {
            **_review_findings_snapshot_dict(result),
            "loop_id": loop_id,
            "action_id": action_id,
        }
    )
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if len(content.encode("utf-8")) > MAX_REVIEW_FINDINGS_SNAPSHOT_BYTES:
        _raise_invalid_snapshot("artifact exceeds size limit")
    artifact_path = lc.save_artifact(
        loop_id,
        project_dir,
        action_id,
        REVIEW_FINDINGS_SNAPSHOT_ARTIFACT,
        content,
    )
    _validate_review_findings_snapshot_action(loop_id, project_dir, action_id, lease_token)
    return artifact_path


def load_review_findings_snapshot(
    loop_id: str,
    project_dir: str,
    action_id: str,
    lease_token: str,
) -> ReviewFindingsResult:
    """Load and strictly validate one action's review findings snapshot."""
    _validate_review_findings_snapshot_action(loop_id, project_dir, action_id, lease_token)
    payload = _load_review_findings_snapshot_artifact(loop_id, project_dir, action_id)
    result = _review_findings_snapshot_from_dict(payload, loop_id, action_id)
    _validate_review_findings_snapshot_action(loop_id, project_dir, action_id, lease_token)
    return result


def verify_origin(raw: dict[str, Any], allowlist: tuple[ReviewerAllowlistEntry, ...]) -> bool:
    """Return True when a GitHub API item is from an allowed automated reviewer."""
    app_slug = _github_app_slug(raw)
    if app_slug and any(_entry_allows_app(entry, app_slug, raw) for entry in allowlist):
        return True
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    login = user.get("login")
    user_type = user.get("type")
    return any(_entry_allows_login(entry, login, user_type, raw) for entry in allowlist)


def classify_severity(
    body: str,
    config: PrReviewConfig,
    *,
    classification_response: str | None = None,
) -> SeverityDecision:
    """Classify severity without invoking any LLM or external agent."""
    explicit = _explicit_severity(body, config)
    if explicit is not None:
        severity, reason = explicit
        return SeverityDecision(severity, "explicit", False, reason)
    if classification_response is None:
        return SeverityDecision("high", "fail_safe", True, "classification_required")
    parsed = _parse_classification_response(classification_response)
    if parsed is None:
        return SeverityDecision("high", "fail_safe", False, "invalid_classification")
    severity, confidence = parsed
    if confidence != "high":
        return SeverityDecision("high", "fail_safe", False, "low_confidence")
    if severity == "none":
        return SeverityDecision(None, "external_classification", False, "not_a_finding")
    return SeverityDecision(severity, "external_classification", False, "classified")


def apply_severity_classifications(
    loop_id: str,
    project_dir: str,
    result: ReviewFindingsResult,
    config: PrReviewConfig,
    classification_responses: dict[str, str],
    iteration: int,
    lease_token: str,
    *,
    action_id: str | None = None,
) -> ClassificationApplicationResult:
    """Apply external classifications to the result and its persistent state."""
    pending = [finding for finding in result.findings if finding.needs_classification]
    if not pending:
        return ClassificationApplicationResult(result, ())

    state = lc.load_state(loop_id, project_dir)
    pr_review = _ensure_pr_review_state(state.pr_review)
    findings_map = _findings_map(pr_review)
    processed = set(_processed_comment_ids(pr_review))
    updated_findings: list[ImportedFinding] = []
    applied: list[AppliedSeverityClassification] = []

    for finding in result.findings:
        if not finding.needs_classification:
            updated_findings.append(finding)
            continue
        decision = classify_severity(
            finding.body_excerpt,
            config,
            classification_response=classification_responses.get(finding.source_comment_id, ""),
        )
        _apply_classification_to_state(findings_map, finding, decision, iteration)
        processed.add(finding.source_comment_id)
        applied.append(
            AppliedSeverityClassification(
                signature=finding.signature,
                source_comment_id=finding.source_comment_id,
                severity=decision.severity,
                source=decision.source,
                reason=decision.reason,
            )
        )
        if decision.severity is not None:
            updated_findings.append(
                ImportedFinding(
                    signature=finding.signature,
                    severity=decision.severity,
                    source_comment_id=finding.source_comment_id,
                    body_excerpt=finding.body_excerpt,
                    path=finding.path,
                    line=finding.line,
                    needs_classification=False,
                )
            )

    pr_review["findings"] = findings_map
    pr_review["processed_comment_ids"] = sorted(processed)
    state.pr_review = pr_review
    state.updated_at = lc.now_iso()
    # Blocking-only, matching `collect_review_findings` (issue #213): classification can move a
    # finding from the "high" fail-safe placeholder down to its confirmed medium/low severity,
    # which must drop it out of the no-progress guard's signature set here too.
    iteration_findings = lc.build_pr_iteration_findings(
        pr_review, iteration, severities=lc.BLOCKING_SEVERITIES
    )
    open_non_blocking = _open_non_blocking_findings(findings_map)
    with _fenced_pr_review_write(
        state, loop_id, project_dir, lease_token, action_id, COLLECT_ACTIONS
    ):
        lc.append_journal_event(
            loop_id,
            project_dir,
            "pr_review_severities_classified",
            "waiter",
            action_id,
            {
                "classifications": [
                    {
                        "signature": item.signature,
                        "source_comment_id": item.source_comment_id,
                        "severity": item.severity,
                        "source": item.source,
                        "reason": item.reason,
                    }
                    for item in applied
                ]
            },
        )
        lc._write_state(state, project_dir)
    updated_result = ReviewFindingsResult(
        findings=tuple(updated_findings),
        iteration_findings=iteration_findings,
        previous_iteration_findings=result.previous_iteration_findings,
        open_non_blocking=open_non_blocking,
        processed_comment_ids=tuple(pr_review["processed_comment_ids"]),
        ignored_untrusted_comment_count=result.ignored_untrusted_comment_count,
        needs_classification_count=0,
    )
    return ClassificationApplicationResult(updated_result, tuple(applied))


def normalize_signature(item: ReviewItem, dedup: DedupConfig) -> str:
    """Normalize one PR review finding into a stable short signature."""
    return lc.normalize_pr_finding_signature(
        _review_item_signature_payload(item), _dedup_dict(dedup)
    )


def build_iteration_findings(pr_review: dict[str, Any], iteration: int) -> IterationFindings:
    """Build current-iteration open signature summary from state.pr_review."""
    return lc.build_pr_iteration_findings(pr_review, iteration)


def evaluate_no_progress(prev: IterationFindings, current: IterationFindings) -> NoProgressResult:
    """Evaluate pr_review_response no-progress using reraised signatures or new_count."""
    return lc.evaluate_pr_review_no_progress(prev, current)


def dismiss_finding(
    loop_id: str,
    project_dir: str,
    signature: str,
    reason: str,
    lease_token: str,
    *,
    decided_by: str = "maker",
    action_id: str | None = None,
) -> None:
    """Dismiss a medium/low finding with a non-empty reason."""
    if not reason.strip():
        raise DismissalError("dismiss reason is required")
    state = lc.load_state(loop_id, project_dir)
    pr_review = _ensure_pr_review_state(state.pr_review)
    findings = _findings_map(pr_review)
    record = findings.get(signature)
    if not isinstance(record, dict):
        raise DismissalError(f"unknown finding signature: {signature}")
    severity = str(record.get("severity") or "")
    if severity in {"critical", "high"}:
        raise DismissalError("critical/high findings cannot be dismissed")
    if severity not in {"medium", "low"}:
        raise DismissalError(f"invalid dismissible severity: {severity}")
    updated = {**record, "status": "dismissed", "dismiss_reason": reason}
    findings[signature] = updated
    pr_review["findings"] = findings
    state.pr_review = pr_review
    state.updated_at = lc.now_iso()
    with _fenced_pr_review_write(
        state, loop_id, project_dir, lease_token, action_id, DISMISS_ACTIONS
    ):
        lc.append_journal_event(
            loop_id,
            project_dir,
            "dismissed",
            decided_by,
            action_id,
            {"signature": signature, "reason": reason, "decided_by": decided_by},
        )
        lc._write_state(state, project_dir)


def mark_addressed_findings(
    loop_id: str,
    project_dir: str,
    signatures: Iterable[str],
    commit_sha: str | None,
    iteration: int,
    lease_token: str,
    *,
    action_id: str | None = None,
) -> tuple[str, ...]:
    """Mark blocking findings not reraised this round as addressed (issue #235).

    Callers pass the signatures that were open (critical/high) at `iteration - 1` but did not
    reappear at `iteration` -- i.e. `previous_iteration_findings.signatures -
    iteration_findings.signatures` from the same `collect_review_findings` result that just
    confirmed no reraise. Only touches records currently `status == "open"`: an already-
    `dismissed` record is never resurrected, and re-marking an already-`addressed` record is a
    harmless idempotent overwrite. This is a pure state update -- it never talks to GitHub;
    the best-effort GitHub thread reply/resolve is `resolve_addressed_findings`'s job.
    """
    candidates = sorted(set(signatures))
    if not candidates:
        return ()
    state = lc.load_state(loop_id, project_dir)
    pr_review = _ensure_pr_review_state(state.pr_review)
    findings_map = _findings_map(pr_review)
    addressed: list[str] = []
    for signature in candidates:
        record = findings_map.get(signature)
        if not isinstance(record, dict) or record.get("status") != "open":
            continue
        findings_map[signature] = {
            **record,
            "status": "addressed",
            "addressed_at_commit": commit_sha,
            "addressed_at_iteration": iteration,
        }
        addressed.append(signature)
    if not addressed:
        return ()
    pr_review["findings"] = findings_map
    state.pr_review = pr_review
    state.updated_at = lc.now_iso()
    with _fenced_pr_review_write(
        state, loop_id, project_dir, lease_token, action_id, COLLECT_ACTIONS
    ):
        lc.append_journal_event(
            loop_id,
            project_dir,
            "pr_review_findings_addressed",
            "waiter",
            action_id,
            {"signatures": addressed, "commit_sha": commit_sha, "iteration": iteration},
        )
        lc._write_state(state, project_dir)
    return tuple(addressed)


def resolve_addressed_findings(
    loop_id: str,
    project_dir: str,
    pr_number: int,
    repo: str,
    signatures: Iterable[str],
    commit_sha: str | None,
    lease_token: str,
    *,
    action_id: str | None = None,
    timeout_seconds: int = DEFAULT_GH_API_TIMEOUT_SECONDS,
) -> AddressedFindingsResult:
    """Best-effort reply + resolve trusted GitHub review threads for addressed findings.

    Reuses `git-workflow`'s `pr_review_threads` module (issue #235) instead of duplicating its
    GraphQL/REST/origin-verification logic. Purely additive/observational: any GitHub API
    failure for one thread is caught and reported in the returned `thread_outcomes`, never
    raised, so a transient GitHub outage can never fail `wait_external_review` itself. Only
    threads whose *every* comment verifies as trusted-bot origin are touched (`fetch_review_threads`
    already drops `has_non_bot_comments` threads' comments; this additionally skips a thread
    outright when any of its comments were dropped, so a mixed bot/human thread is never
    resolved out from under a human reviewer's own commentary).

    PR #276 review (P2, round 2): `_fenced_pr_review_write` only fences the *state* write this
    function makes at the end (recording `resolved_thread_ids`) -- it never guards the GitHub
    `reply_to_comment`/`resolve_thread` calls themselves, which used to run unconditionally even
    after the caller-held `lease_token` had expired or been reacquired by another worker. This
    reuses `lc.validate_lease` (the same non-raising check `guarded_lease_section` builds on) to
    check the lease immediately before the reply/resolve loop starts, and again before each
    individual thread's GitHub writes, so a lease that goes stale mid-call stops issuing further
    GitHub side effects instead of racing a new lease holder. Every signature/thread this call
    never got to because of an expired lease is reported as `"lease_expired"` in
    `thread_outcomes`, never silently dropped.
    """
    candidates = sorted(set(signatures))
    if not candidates:
        return AddressedFindingsResult((), (), git_workflow_unavailable=False)
    git_workflow = _load_git_workflow_module()
    if git_workflow is None:
        return AddressedFindingsResult((), (), git_workflow_unavailable=True)
    owner, _, name = repo.partition("/")
    try:
        fetch_result = git_workflow.fetch_review_threads(pr_number, project_dir, timeout_seconds)
    except Exception:  # noqa: BLE001 - best-effort GitHub read, never fail the caller
        return AddressedFindingsResult((), (), git_workflow_unavailable=False)
    # PR #276 review (medium): `fetch_review_threads()` degrades to an *unfiltered* result
    # (every comment treated as trusted, `has_non_bot_comments` always False) and reports
    # this via `origin_verified: False` whenever it cannot load the reviewer allowlist config
    # or import `pr_review_wait` itself. Skip resolving anything in that case -- fail-closed,
    # matching `fetch_review_threads`'s own bot-origin verification contract -- rather than
    # reply-and-resolve threads whose bot origin was never actually verified.
    if not isinstance(fetch_result, dict) or not fetch_result.get("origin_verified"):
        return AddressedFindingsResult((), (), git_workflow_unavailable=False)
    comment_to_thread = _index_trusted_threads_by_comment(fetch_result)

    state = lc.load_state(loop_id, project_dir)
    pr_review = _ensure_pr_review_state(state.pr_review)
    findings_map = _findings_map(pr_review)

    def lease_still_valid() -> bool:
        return lc.validate_lease(loop_id, project_dir, lease_token)

    outcomes: list[AddressedThreadOutcome] = []
    newly_resolved_thread_ids: set[str] = set()
    updated_signatures: list[str] = []
    # PR #276 review (P2, round 2): re-check right before this GitHub write loop starts (not
    # only once at function entry) so a lease that was already stale by the time the earlier
    # fetch/state-load above finished is caught before the first reply/resolve call, not after.
    lease_expired = not lease_still_valid()
    for signature in candidates:
        if lease_expired:
            outcomes.append(AddressedThreadOutcome(signature, None, None, "lease_expired"))
            continue
        record = findings_map.get(signature)
        if not isinstance(record, dict):
            continue
        signature_outcomes = _resolve_addressed_signature_threads(
            git_workflow,
            owner,
            name,
            pr_number,
            signature,
            record,
            comment_to_thread,
            newly_resolved_thread_ids,
            commit_sha,
            timeout_seconds,
            lease_still_valid,
        )
        outcomes.extend(signature_outcomes)
        if any(outcome.status == "lease_expired" for outcome in signature_outcomes):
            # The lease went stale partway through this signature's threads (checked again
            # inside `_resolve_addressed_signature_threads`, right before each GitHub write) --
            # stop attempting any further signature's reply/resolve for the rest of this call.
            lease_expired = True
        newly_resolved_for_signature = {
            outcome.thread_id
            for outcome in signature_outcomes
            if outcome.status == "resolved" and outcome.thread_id is not None
        }
        if newly_resolved_for_signature:
            newly_resolved_thread_ids |= newly_resolved_for_signature
            already_resolved = set(record.get("resolved_thread_ids") or [])
            findings_map[signature] = {
                **record,
                "resolved_thread_ids": sorted(already_resolved | newly_resolved_for_signature),
            }
            updated_signatures.append(signature)

    if updated_signatures:
        pr_review["findings"] = findings_map
        state.pr_review = pr_review
        state.updated_at = lc.now_iso()
        with _fenced_pr_review_write(
            state, loop_id, project_dir, lease_token, action_id, COLLECT_ACTIONS
        ):
            lc.append_journal_event(
                loop_id,
                project_dir,
                "pr_review_threads_resolved",
                "waiter",
                action_id,
                {
                    "signatures": updated_signatures,
                    "thread_ids": sorted(newly_resolved_thread_ids),
                },
            )
            lc._write_state(state, project_dir)
    return AddressedFindingsResult(
        tuple(updated_signatures), tuple(outcomes), git_workflow_unavailable=False
    )


_GIT_WORKFLOW_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "git-workflow" / "scripts"


def _load_git_workflow_module() -> Any | None:
    """Import git-workflow's `pr_review_threads` module if the package is present (issue #235).

    Mirrors `pr_review_threads._import_pr_review_wait`'s optional-import-with-sys.path-insert
    pattern in the opposite direction. Absence (e.g. `git-workflow` not installed in this
    project) degrades `resolve_addressed_findings` to a no-op, never to a hard failure.
    """
    scripts_dir = str(_GIT_WORKFLOW_SCRIPTS_DIR)
    if _GIT_WORKFLOW_SCRIPTS_DIR.is_dir() and scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        import pr_review_threads  # type: ignore[import-not-found]
    except ImportError:
        return None
    return pr_review_threads


def _index_trusted_threads_by_comment(fetch_result: Any) -> dict[int, dict[str, Any]]:
    """Index `fetch_review_threads()`'s unresolved, fully-bot-origin threads by comment id."""
    if not isinstance(fetch_result, dict) or "error" in fetch_result:
        return {}
    unresolved_threads = fetch_result.get("unresolved_threads")
    if not isinstance(unresolved_threads, list):
        return {}
    comment_to_thread: dict[int, dict[str, Any]] = {}
    for thread in unresolved_threads:
        if not isinstance(thread, dict) or thread.get("has_non_bot_comments"):
            continue
        for comment in thread.get("comments") or []:
            if not isinstance(comment, dict):
                continue
            comment_id = comment.get("comment_id")
            if isinstance(comment_id, int):
                comment_to_thread[comment_id] = thread
    return comment_to_thread


def _review_comment_ids(record: dict[str, Any]) -> list[int]:
    """Return every `review_comment:<id>` numeric id recorded for one finding, ascending.

    PR #276 review (high): a reraised finding accumulates one `review_comment:<id>` entry
    per GitHub comment it appeared in (`_upsert_finding` unions `source_comment_ids` across
    reraises), and different reraises can land in genuinely different GitHub threads.
    Resolving only the single highest id -- as this used to -- left every earlier thread
    permanently unresolved; callers must resolve every id this returns.
    """
    ids = record.get("source_comment_ids")
    if not isinstance(ids, list):
        return []
    review_comment_ids: list[int] = []
    for raw in ids:
        kind, _, value = str(raw).partition(":")
        if kind != "review_comment":
            continue
        try:
            review_comment_ids.append(int(value))
        except ValueError:
            continue
    return sorted(review_comment_ids)


def _reply_target_for_comment(thread: dict[str, Any], comment_id: int) -> int | None:
    """Return the root comment id a reply to `comment_id` in `thread` must target."""
    for comment in thread.get("comments") or []:
        if isinstance(comment, dict) and comment.get("comment_id") == comment_id:
            target = comment.get("reply_target_id")
            return target if isinstance(target, int) else None
    return None


def _resolve_addressed_signature_threads(
    git_workflow: Any,
    owner: str,
    name: str,
    pr_number: int,
    signature: str,
    record: dict[str, Any],
    comment_to_thread: dict[int, dict[str, Any]],
    already_resolved_this_call: set[str],
    commit_sha: str | None,
    timeout_seconds: int,
    lease_still_valid: Callable[[], bool],
) -> list[AddressedThreadOutcome]:
    """Reply to and resolve every trusted GitHub thread accumulated for one addressed finding.

    Returns one `AddressedThreadOutcome` per distinct thread id the record's `review_comment:`
    ids map to (deduplicated -- multiple ids can land in the same thread), or a single
    `no_review_comment_source` outcome when the record carries no `review_comment:` id at all.

    PR #276 review (P2, round 2): `lease_still_valid()` is re-checked immediately before every
    individual thread's `reply_to_comment`/`resolve_thread` GitHub writes (not just once per
    signature by the caller) -- once it returns `False`, no further GitHub write is attempted
    for this signature's remaining threads either; each is reported `"lease_expired"` instead.
    """
    comment_ids = _review_comment_ids(record)
    if not comment_ids:
        return [AddressedThreadOutcome(signature, None, None, "no_review_comment_source")]
    already_resolved = set(record.get("resolved_thread_ids") or [])
    seen_thread_ids: set[str] = set()
    outcomes: list[AddressedThreadOutcome] = []
    lease_expired = False
    for comment_id in comment_ids:
        thread = comment_to_thread.get(comment_id)
        if thread is None:
            outcomes.append(
                AddressedThreadOutcome(signature, None, comment_id, "no_trusted_thread")
            )
            continue
        thread_id = thread.get("thread_id")
        if thread_id in seen_thread_ids:
            # Another id already resolved (or attempted) this same thread earlier in this
            # loop -- multiple review comments can belong to one discussion/thread.
            continue
        seen_thread_ids.add(thread_id)
        if thread_id in already_resolved or thread_id in already_resolved_this_call:
            outcomes.append(
                AddressedThreadOutcome(signature, thread_id, comment_id, "already_resolved")
            )
            continue
        if lease_expired:
            outcomes.append(
                AddressedThreadOutcome(signature, thread_id, comment_id, "lease_expired")
            )
            continue
        if not lease_still_valid():
            lease_expired = True
            outcomes.append(
                AddressedThreadOutcome(signature, thread_id, comment_id, "lease_expired")
            )
            continue
        outcomes.append(
            _resolve_one_trusted_thread(
                git_workflow,
                owner,
                name,
                pr_number,
                signature,
                thread,
                comment_id,
                commit_sha,
                timeout_seconds,
            )
        )
    return outcomes


def _resolve_one_trusted_thread(
    git_workflow: Any,
    owner: str,
    name: str,
    pr_number: int,
    signature: str,
    thread: dict[str, Any],
    comment_id: int,
    commit_sha: str | None,
    timeout_seconds: int,
) -> AddressedThreadOutcome:
    """Reply to and resolve one already-identified trusted GitHub thread, best-effort."""
    thread_id = thread.get("thread_id")
    reply_target = _reply_target_for_comment(thread, comment_id) or comment_id
    body = _addressed_reply_body(commit_sha)
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix="loop-harness-pr-review-reply-", suffix=".md")
        os.close(fd)
        tmp_path = Path(tmp_name)
        tmp_path.chmod(0o600)
        tmp_path.write_text(body, encoding="utf-8")
        git_workflow.reply_to_comment(
            owner,
            name,
            pr_number,
            reply_target,
            tmp_path,
            issue_comment=False,
            timeout=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort GitHub write, never raise
        return AddressedThreadOutcome(signature, thread_id, comment_id, "reply_failed", str(exc))
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    try:
        git_workflow.resolve_thread(thread_id, timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - best-effort GitHub write, never raise
        return AddressedThreadOutcome(signature, thread_id, comment_id, "resolve_failed", str(exc))
    return AddressedThreadOutcome(signature, thread_id, comment_id, "resolved")


def _addressed_reply_body(commit_sha: str | None) -> str:
    """Build the reply body posted to a trusted thread whose finding was not reraised."""
    short_sha = commit_sha[:7] if commit_sha else "unknown"
    return (
        f"Addressed in commit `{short_sha}`. This finding was not reraised in the latest "
        "automated review round; resolving this thread."
    )


def phase_check_from_review_findings(result: ReviewFindingsResult) -> lc.PhaseCheckResult:
    """Convert imported PR review findings into the shared checker result shape.

    `passed` (both the phase-level and single-check-level flag) reflects only blocking
    (critical/high) findings (issue #213): a result with only medium/low findings is
    `passed=true`, so `pr_review_response` has an exit path even when nobody has dismissed
    every non-blocking comment. `findings` still carries every severity for observability (the
    Maker-facing filter to critical/high happens downstream, see
    `loop_driver._pr_review_findings_from_last_check`). `metadata["non_blocking_open"]`
    separately reports every currently open medium/low finding for exit-time reporting.
    """
    findings = [
        lc.Finding(
            severity=item.severity,
            summary=item.body_excerpt,
            source="pr_review",
            path=item.path,
            line=item.line,
        )
        for item in result.findings
    ]
    blocking = [item for item in findings if item.severity in lc.BLOCKING_SEVERITIES]
    signature = lc.compute_pr_review_signature(list(result.iteration_findings.signatures))
    check = lc.CheckResult(
        passed=not blocking,
        layer="llm_review",
        signature=signature,
        findings=findings,
        raw_artifact_path="",
    )
    return lc.PhaseCheckResult(
        passed=not blocking,
        results=[check],
        signature=signature,
        infrastructure_failure=False,
        metadata={
            "previous_iteration_findings": _iteration_findings_dict(
                result.previous_iteration_findings
            ),
            "current_iteration_findings": _iteration_findings_dict(result.iteration_findings),
            "non_blocking_open": [
                _non_blocking_finding_dict(item) for item in result.open_non_blocking
            ],
        },
    )


def phase_check_from_completion_outcome(outcome: CompletionOutcome) -> lc.PhaseCheckResult:
    """Represent timeout/API polling outcomes as checker-compatible results."""
    metadata = _completion_outcome_metadata(outcome)
    if outcome.infrastructure_failure:
        return lc.PhaseCheckResult(False, [], "pr_review_api_error", True, metadata=metadata)
    if outcome.signal == "reviewer_unavailable":
        return lc.PhaseCheckResult(
            False, [], "external_reviewer_unavailable", False, metadata=metadata
        )
    if outcome.timed_out:
        return lc.PhaseCheckResult(False, [], "pr_review_timeout", False, metadata=metadata)
    return lc.PhaseCheckResult(True, [], "", False, metadata=metadata)


def record_ignored_untrusted_reviews(
    loop_id: str,
    project_dir: str,
    outcome: CompletionOutcome,
    lease_token: str,
    *,
    action_id: str | None = None,
) -> int:
    """Persist untrusted submitted reviews observed while polling."""
    if not outcome.ignored_untrusted_reviews:
        return 0
    state = lc.load_state(loop_id, project_dir)
    pr_review = _ensure_pr_review_state(state.pr_review)
    processed = set(_processed_comment_ids(pr_review))
    new_items = [
        item
        for item in outcome.ignored_untrusted_reviews
        if _ignored_review_key(item) not in processed
    ]
    if not new_items:
        return 0
    processed.update(_ignored_review_key(item) for item in new_items)
    pr_review["processed_comment_ids"] = sorted(processed)
    state.pr_review = pr_review
    state.ignored_untrusted_comment_count += len(new_items)
    state.updated_at = lc.now_iso()
    with _fenced_pr_review_write(
        state, loop_id, project_dir, lease_token, action_id, COLLECT_ACTIONS
    ):
        for item in new_items:
            _journal_ignored_untrusted_review(loop_id, project_dir, action_id, item)
        lc._write_state(state, project_dir)
    return len(new_items)


@contextmanager
def _fenced_pr_review_write(
    state: lc.LoopState,
    loop_id: str,
    project_dir: str,
    lease_token: str,
    action_id: str | None,
    allowed_actions: frozenset[str],
) -> Iterator[None]:
    """Fence an auxiliary state write against the active pending action (DH2).

    The previous `_fence_state_update` validated the lease (`lc._ensure_valid_lease`) and,
    when `action_id` is set, re-read state to check for staleness -- but returned before the
    caller's journal/state write, leaving that write unguarded. A lease can expire and be
    reacquired by another worker in the gap between this validation and the write, letting a
    stale worker's write land after a new owner has already started mutating state (the same
    class of TOCTOU DH1 closes for `loop_common`'s own propose/complete/reconcile). Wrapping
    validation *and* the caller's write inside `loop_common.guarded_lease_section`'s held
    flock closes this window for every `pr_review_wait` state mutator.
    """
    with lc.guarded_lease_section(loop_id, project_dir, lease_token):
        _validate_pr_review_fence(state, loop_id, project_dir, action_id, allowed_actions)
        yield


def _validate_pr_review_fence(
    state: lc.LoopState,
    loop_id: str,
    project_dir: str,
    action_id: str | None,
    allowed_actions: frozenset[str],
) -> None:
    """Validate the pending action/state_version fencing for an auxiliary state write."""
    if action_id is None:
        state.state_version += 1
        return
    fresh = lc.load_state(loop_id, project_dir)
    pending = fresh.pending_action
    if (
        fresh.state_version != state.state_version
        or pending is None
        or pending.action_id != action_id
        or pending.phase != fresh.phase
    ):
        raise lc.StaleActionError(f"stale PR review action: {action_id}")
    if pending.action not in allowed_actions:
        raise lc.ProtocolViolationError(f"action {pending.action} cannot update PR review state")


def _fetch_reviews(client: GhApiClient, pr_number: int) -> list[dict[str, Any]]:
    return _list_payload(client.api(f"repos/{client.repo}/pulls/{pr_number}/reviews"))


def _fetch_review_comments(client: GhApiClient, pr_number: int) -> list[dict[str, Any]]:
    return _list_payload(client.api(f"repos/{client.repo}/pulls/{pr_number}/comments"))


def _fetch_issue_comments(client: GhApiClient, pr_number: int) -> list[dict[str, Any]]:
    return _list_payload(client.api(f"repos/{client.repo}/issues/{pr_number}/comments"))


def _fetch_all_review_items(client: GhApiClient, pr_number: int) -> list[ReviewItem]:
    return [
        *(_review_item_from_review(item) for item in _fetch_reviews(client, pr_number)),
        *(
            _review_item_from_review_comment(item)
            for item in _fetch_review_comments(client, pr_number)
        ),
        *(
            _review_item_from_issue_comment(item)
            for item in _fetch_issue_comments(client, pr_number)
        ),
    ]


def fetch_review_items(client: GhApiClient, pr_number: int) -> list[ReviewItem]:
    """Fetch and normalize one snapshot of reviews/review-comments/issue-comments (DC3).

    Public so a caller (e.g. `loop_driver._drain_before_push`) can fetch exactly once and
    pass the same snapshot into both `collect_review_findings` and `record_baseline`,
    instead of each function fetching independently and leaving a race window between the
    two fetches where an in-between comment is silently dropped.
    """
    return _fetch_all_review_items(client, pr_number)


def _loads_paginated_json(output: str) -> Any:
    """Parse single or concatenated JSON documents from `gh api --paginate`."""
    decoder = json.JSONDecoder()
    values: list[Any] = []
    index = 0
    text = output.strip()
    while index < len(text):
        value, index = decoder.raw_decode(text, index)
        values.append(value)
        while index < len(text) and text[index].isspace():
            index += 1
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    if all(isinstance(value, list) for value in values):
        merged: list[Any] = []
        for value in values:
            merged.extend(value)
        return merged
    if all(isinstance(value, dict) and "check_runs" in value for value in values):
        merged_runs: list[Any] = []
        for value in values:
            merged_runs.extend(value.get("check_runs") or [])
        return {"check_runs": merged_runs}
    return values


def _review_completion_outcome(
    pr_number: int,
    baseline: dict[str, Any],
    config: PrReviewConfig,
    client: GhApiClient,
    ignored_reviews: dict[str, IgnoredUntrustedReview],
) -> CompletionOutcome:
    baseline_id = _int_or_zero(baseline.get("baseline_review_id"))
    # DH4: a review's `id` sorting after `baseline_id` only proves it is *new*, not that it
    # reviewed *this iteration's* just-pushed commit -- GitHub lets a review be submitted
    # against a stale diff (e.g. a reviewer with a stale page open). When this iteration's
    # push has recorded a head sha, require the review's `commit_id` to match it before
    # treating the review as a completion signal for this iteration.
    iteration_head_sha = _optional_str(baseline.get("iteration_head_sha"))
    reviews = _fetch_reviews(client, pr_number)
    trusted_ids: list[int] = []
    for item in reviews:
        review_id = _int_or_zero(item.get("id"))
        if review_id <= baseline_id:
            continue
        if not _is_submitted_review(item):
            continue
        if not verify_origin(item, config.reviewer_allowlist):
            ignored_reviews.setdefault(
                f"review:{review_id}", _ignored_untrusted_review_from_review(item, review_id)
            )
            continue
        if iteration_head_sha is not None and not _review_matches_iteration_head(
            item, iteration_head_sha
        ):
            # Fail-safe, not fail-open: a review whose `commit_id` is missing or does not
            # match this iteration's head is simply not adopted as a completion signal (it
            # is still a legitimate review -- just not evidence *this* iteration's fix was
            # reviewed), rather than being waved through because it happens to be trusted.
            continue
        trusted_ids.append(review_id)
    if not trusted_ids:
        return CompletionOutcome(
            "pending",
            completed=False,
            timed_out=False,
            infrastructure_failure=False,
            ignored_untrusted_review_count=len(ignored_reviews),
            ignored_untrusted_reviews=_ignored_review_tuple(ignored_reviews),
        )
    return CompletionOutcome(
        "review_submitted",
        completed=True,
        timed_out=False,
        infrastructure_failure=False,
        review_ids=tuple(sorted(trusted_ids)),
        ignored_untrusted_review_count=len(ignored_reviews),
        ignored_untrusted_reviews=_ignored_review_tuple(ignored_reviews),
    )


def _review_matches_iteration_head(raw: dict[str, Any], iteration_head_sha: str) -> bool:
    """Return True only when a review's `commit_id` is known and matches (DH4)."""
    commit_id = _optional_str(raw.get("commit_id"))
    if commit_id is None:
        return False
    return commit_id == iteration_head_sha


def _is_issue_comment_completion_signal(
    item: ReviewItem, baseline: dict[str, Any], config: PrReviewConfig
) -> bool:
    """Return True when a trusted, post-baseline issue comment is a terminal review verdict."""
    if _comment_key(item) in set(_string_list(baseline.get("processed_comment_ids"))):
        return False
    if not verify_origin(item.raw, config.reviewer_allowlist):
        return False
    if not _is_after_baseline_time(item, str(baseline.get("baseline_recorded_at") or "")):
        return False
    if _is_auto_generated_comment(item.body, config):
        return False
    iteration_sha = baseline.get("iteration_head_sha")
    if isinstance(iteration_sha, str) and iteration_sha and iteration_sha[:7] not in item.body:
        return False
    return _matches_terminal_verdict(item.body)


def _matches_terminal_verdict(body: str) -> bool:
    """Return True when body casefold-contains a terminal review verdict phrase."""
    normalized = (body or "").casefold()
    return any(pattern.casefold() in normalized for pattern in TERMINAL_VERDICT_PATTERNS)


def _issue_comment_completion_outcome(
    pr_number: int, baseline: dict[str, Any], config: PrReviewConfig, client: GhApiClient
) -> CompletionOutcome:
    """Return a completion outcome from trusted issue comments carrying a terminal verdict."""
    if not baseline.get("baseline_recorded_at"):
        return CompletionOutcome(
            "pending", completed=False, timed_out=False, infrastructure_failure=False
        )
    items = [
        _review_item_from_issue_comment(raw) for raw in _fetch_issue_comments(client, pr_number)
    ]
    matched = [
        item for item in items if _is_issue_comment_completion_signal(item, baseline, config)
    ]
    if not matched:
        return CompletionOutcome(
            "pending", completed=False, timed_out=False, infrastructure_failure=False
        )
    return CompletionOutcome(
        "issue_comment_completed",
        completed=True,
        timed_out=False,
        infrastructure_failure=False,
        issue_comment_ids=tuple(sorted(_comment_key(item) for item in matched)),
    )


def _reviewer_unavailable_outcome(
    pr_number: int, baseline: dict[str, Any], config: PrReviewConfig, client: GhApiClient
) -> CompletionOutcome | None:
    """Return a safe outcome for trusted CodeRabbit rate-limit replies."""
    if not baseline.get("baseline_recorded_at"):
        return None
    items = [
        _review_item_from_issue_comment(raw) for raw in _fetch_issue_comments(client, pr_number)
    ]
    matched = [item for item in items if _is_reviewer_unavailable_signal(item, baseline, config)]
    if not matched:
        return None
    return CompletionOutcome(
        "reviewer_unavailable",
        completed=True,
        timed_out=False,
        infrastructure_failure=False,
        reviewer_unavailable_comment_ids=tuple(sorted(_comment_key(item) for item in matched)),
        reviewer_unavailable_reason=REVIEWER_UNAVAILABLE_REASON,
    )


def _is_reviewer_unavailable_signal(
    item: ReviewItem, baseline: dict[str, Any], config: PrReviewConfig
) -> bool:
    """Match only trusted, post-baseline, unprocessed CodeRabbit rate-limit replies."""
    if _comment_key(item) in set(_string_list(baseline.get("processed_comment_ids"))):
        return False
    coderabbit_entries = tuple(
        entry for entry in config.reviewer_allowlist if _is_coderabbit_entry(entry)
    )
    if not coderabbit_entries or not verify_origin(item.raw, coderabbit_entries):
        return False
    if not _is_after_baseline_time(item, str(baseline.get("baseline_recorded_at") or "")):
        return False
    normalized = item.body.casefold()
    has_marker = any(
        _is_coderabbit_rate_limit_marker(marker) and marker.casefold() in normalized
        for marker in config.auto_generated_markers
    )
    has_rate_limit_phrase = any(pattern in normalized for pattern in RATE_LIMIT_PATTERNS)
    return has_marker and has_rate_limit_phrase


def _is_coderabbit_rate_limit_marker(marker: str) -> bool:
    """Accept only CodeRabbit markers that explicitly represent an unavailable reply."""
    normalized = marker.casefold()
    return "coderabbit" in normalized and (
        "rate limit" in normalized or "auto-generated reply by coderabbit" in normalized
    )


def _is_coderabbit_entry(entry: ReviewerAllowlistEntry) -> bool:
    """Return True when an allowlist entry identifies CodeRabbit."""
    identities = (entry.app_slug, entry.login)
    return any("coderabbit" in value.casefold() for value in identities if value)


def _has_alternate_review_path(config: PrReviewConfig) -> bool:
    """Return True when a non-CodeRabbit reviewer or check-run can still complete."""
    return bool(config.checkrun_allowlist) or any(
        not _is_coderabbit_entry(entry) for entry in config.reviewer_allowlist
    )


def _checkrun_completion_outcome(
    baseline: dict[str, Any], config: PrReviewConfig, client: GhApiClient
) -> CompletionOutcome:
    if not config.checkrun_allowlist:
        return CompletionOutcome("pending", False, False, False)
    sha = baseline.get("iteration_head_sha")
    if not isinstance(sha, str) or not sha:
        return CompletionOutcome("pending", False, False, False)
    payload = client.api(f"repos/{client.repo}/commits/{sha}/check-runs")
    runs = payload.get("check_runs") if isinstance(payload, dict) else payload
    allowlisted = [
        item
        for item in _list_payload(runs)
        if isinstance(item.get("name"), str) and item["name"] in config.checkrun_allowlist
    ]
    if not allowlisted or any(item.get("status") != "completed" for item in allowlisted):
        return CompletionOutcome("pending", False, False, False)
    return CompletionOutcome(
        "check_run_completed",
        completed=True,
        timed_out=False,
        infrastructure_failure=False,
        check_run_names=tuple(sorted(str(item["name"]) for item in allowlisted)),
    )


def _completion_outcome_metadata(outcome: CompletionOutcome) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if outcome.ignored_untrusted_review_count:
        metadata["ignored_untrusted_review_count"] = outcome.ignored_untrusted_review_count
    if outcome.ignored_untrusted_reviews:
        metadata["ignored_untrusted_reviews"] = [
            _ignored_untrusted_review_dict(item) for item in outcome.ignored_untrusted_reviews
        ]
    if outcome.issue_comment_ids:
        metadata["issue_comment_ids"] = list(outcome.issue_comment_ids)
    if outcome.reviewer_unavailable_comment_ids:
        metadata["reviewer_unavailable_comment_ids"] = list(
            outcome.reviewer_unavailable_comment_ids
        )
    if outcome.reviewer_unavailable_reason:
        metadata["reviewer_unavailable_reason"] = outcome.reviewer_unavailable_reason
    if outcome.error:
        metadata["error"] = outcome.error
    if outcome.shortcut_reason:
        metadata["shortcut_reason"] = outcome.shortcut_reason
    if outcome.local_head_sha:
        metadata["local_head_sha"] = outcome.local_head_sha
    if outcome.iteration_head_sha:
        metadata["iteration_head_sha"] = outcome.iteration_head_sha
    return metadata


def _completion_outcome_with_ignored_reviews(
    outcome: CompletionOutcome,
    ignored_reviews: dict[str, IgnoredUntrustedReview],
) -> CompletionOutcome:
    if not ignored_reviews:
        return outcome
    return CompletionOutcome(
        outcome.signal,
        completed=outcome.completed,
        timed_out=outcome.timed_out,
        infrastructure_failure=outcome.infrastructure_failure,
        review_ids=outcome.review_ids,
        check_run_names=outcome.check_run_names,
        issue_comment_ids=outcome.issue_comment_ids,
        reviewer_unavailable_comment_ids=outcome.reviewer_unavailable_comment_ids,
        reviewer_unavailable_reason=outcome.reviewer_unavailable_reason,
        ignored_untrusted_review_count=len(ignored_reviews),
        ignored_untrusted_reviews=_ignored_review_tuple(ignored_reviews),
        error=outcome.error,
        shortcut_reason=outcome.shortcut_reason,
        local_head_sha=outcome.local_head_sha,
        iteration_head_sha=outcome.iteration_head_sha,
    )


def _local_head_sha(worktree_path: str) -> str | None:
    """Return worktree HEAD sha, or None when git cannot resolve it."""
    try:
        completed = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=lc.GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return _optional_str(completed.stdout.strip())


def _ignored_untrusted_review_dict(item: IgnoredUntrustedReview) -> dict[str, Any]:
    return {
        "review_id": item.review_id,
        "login": item.login,
        "author_association": item.author_association,
        "submitted_at": item.submitted_at,
        "body_excerpt": item.body_excerpt,
    }


def _ignored_review_tuple(
    ignored_reviews: dict[str, IgnoredUntrustedReview],
) -> tuple[IgnoredUntrustedReview, ...]:
    return tuple(sorted(ignored_reviews.values(), key=lambda item: item.review_id))


def _ignored_untrusted_review_from_review(
    raw: dict[str, Any], review_id: int
) -> IgnoredUntrustedReview:
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    return IgnoredUntrustedReview(
        review_id=review_id,
        login=_optional_str(user.get("login")),
        author_association=_optional_str(raw.get("author_association")),
        submitted_at=_optional_str(raw.get("submitted_at")),
        body_excerpt=_redacted_excerpt(str(raw.get("body") or "")),
    )


def _is_submitted_review(raw: dict[str, Any]) -> bool:
    if not _optional_str(raw.get("submitted_at")):
        return False
    return str(raw.get("state") or "").upper() != "PENDING"


def _finding_from_item(
    item: ReviewItem, key: str, config: PrReviewConfig, iteration: int
) -> ImportedFinding | None:
    if not item.body.strip():
        return None
    if _is_auto_generated_comment(item.body, config):
        return None
    if _is_positive_review_summary(item):
        return None
    severity = classify_severity(item.body, config)
    signature = normalize_signature(item, config.dedup)
    return ImportedFinding(
        signature=signature,
        severity=severity.severity,
        source_comment_id=key,
        body_excerpt=_redacted_excerpt(item.body),
        path=item.path,
        line=item.line if item.line is not None else item.original_line,
        needs_classification=severity.needs_classification,
    )


def _is_auto_generated_comment(body: str, config: PrReviewConfig) -> bool:
    """Return True when body contains a configured bot auto-generated comment marker."""
    if not body or not config.auto_generated_markers:
        return False
    normalized_body = body.casefold()
    return any(marker.casefold() in normalized_body for marker in config.auto_generated_markers)


def _is_positive_review_summary(item: ReviewItem) -> bool:
    if item.source not in {"review", "issue_comment"}:
        return False
    normalized = re.sub(r"\s+", " ", item.body).strip().casefold().rstrip(".!")
    return normalized in POSITIVE_REVIEW_SUMMARIES or (
        item.source == "issue_comment" and _matches_terminal_verdict(item.body)
    )


def _review_item_signature_payload(item: ReviewItem) -> dict[str, Any]:
    """Convert ReviewItem to loop_common's signature input shape."""
    return {
        "body": item.body,
        "path": item.path,
        "line": item.line,
        "original_line": item.original_line,
    }


def _dedup_dict(dedup: DedupConfig) -> dict[str, Any]:
    """Convert DedupConfig to loop_common's plain dict config."""
    return {
        "line_bucket_size": dedup.line_bucket_size,
        "stopwords_en": sorted(dedup.stopwords_en),
        "stopwords_ja": sorted(dedup.stopwords_ja),
        "signature_footer_patterns": list(dedup.signature_footer_patterns),
    }


def _iteration_findings_dict(value: IterationFindings) -> dict[str, Any]:
    """Serialize IterationFindings for PhaseCheckResult metadata."""
    return {"signatures": sorted(value.signatures), "new_count": value.new_count}


def _non_blocking_finding_dict(value: NonBlockingFinding) -> dict[str, Any]:
    """Serialize NonBlockingFinding for PhaseCheckResult metadata / snapshot payloads."""
    return {
        "signature": value.signature,
        "severity": value.severity,
        "path": value.path,
        "line": value.line,
        "body_excerpt": value.body_excerpt,
    }


def _review_findings_snapshot_dict(result: ReviewFindingsResult) -> dict[str, Any]:
    """Serialize the complete action-scoped review findings result."""
    return {
        "schema_version": REVIEW_FINDINGS_SNAPSHOT_SCHEMA_VERSION,
        "findings": [
            {
                "signature": item.signature,
                "severity": item.severity,
                "source_comment_id": item.source_comment_id,
                "body_excerpt": item.body_excerpt,
                "path": item.path,
                "line": item.line,
                "needs_classification": item.needs_classification,
            }
            for item in result.findings
        ],
        "iteration_findings": _iteration_findings_dict(result.iteration_findings),
        "previous_iteration_findings": _iteration_findings_dict(result.previous_iteration_findings),
        "open_non_blocking": [
            _non_blocking_finding_dict(item) for item in result.open_non_blocking
        ],
        "processed_comment_ids": list(result.processed_comment_ids),
        "ignored_untrusted_comment_count": result.ignored_untrusted_comment_count,
        "needs_classification_count": result.needs_classification_count,
    }


_SNAPSHOT_BASE_FIELDS = frozenset(
    {
        "schema_version",
        "loop_id",
        "action_id",
        "findings",
        "iteration_findings",
        "previous_iteration_findings",
        "processed_comment_ids",
        "ignored_untrusted_comment_count",
        "needs_classification_count",
    }
)
# v1 predates `open_non_blocking` (#213/PR#228 review). An in-flight loop mid-
# `wait_external_review` may have durably persisted a v1 snapshot before this package was
# upgraded; failing closed on it would strand that loop unable to resume/classify, so v1
# payloads are still readable (defaulting `open_non_blocking` to `()`). A v1 payload that
# *does* carry an `open_non_blocking` key is still rejected below (unknown key for that
# version) -- v1's own key set stays exact, only its absence is tolerated.
_LEGACY_SNAPSHOT_SCHEMA_VERSIONS = frozenset({1})


def _review_findings_snapshot_from_dict(
    value: Any, loop_id: str, action_id: str
) -> ReviewFindingsResult:
    """Strictly deserialize an action-scoped review findings snapshot (v1 or current)."""
    if not isinstance(value, dict):
        _raise_invalid_snapshot("snapshot has invalid fields")
    raw_schema_version = value.get("schema_version")
    is_legacy = (
        isinstance(raw_schema_version, int)
        and not isinstance(raw_schema_version, bool)
        and raw_schema_version in _LEGACY_SNAPSHOT_SCHEMA_VERSIONS
    )
    expected_keys = (
        _SNAPSHOT_BASE_FIELDS if is_legacy else _SNAPSHOT_BASE_FIELDS | {"open_non_blocking"}
    )
    data = _snapshot_mapping(value, expected_keys, "snapshot")
    if _snapshot_string(data["loop_id"], "loop_id") != loop_id:
        _raise_invalid_snapshot("loop_id does not match the requested loop")
    if _snapshot_string(data["action_id"], "action_id") != action_id:
        _raise_invalid_snapshot("action_id does not match the requested action")
    schema_version = _snapshot_non_negative_int(data["schema_version"], "schema_version")
    if schema_version not in (
        *_LEGACY_SNAPSHOT_SCHEMA_VERSIONS,
        REVIEW_FINDINGS_SNAPSHOT_SCHEMA_VERSION,
    ):
        _raise_invalid_snapshot("unsupported schema_version")
    raw_findings = data["findings"]
    if not isinstance(raw_findings, list):
        _raise_invalid_snapshot("findings must be a list")
    findings = tuple(
        _snapshot_imported_finding(item, index) for index, item in enumerate(raw_findings)
    )
    needs_classification_count = _snapshot_non_negative_int(
        data["needs_classification_count"], "needs_classification_count"
    )
    actual_pending_count = sum(item.needs_classification for item in findings)
    if needs_classification_count != actual_pending_count:
        _raise_invalid_snapshot("needs_classification_count does not match findings")
    open_non_blocking = (
        ()
        if schema_version in _LEGACY_SNAPSHOT_SCHEMA_VERSIONS
        else _snapshot_open_non_blocking(data["open_non_blocking"], "open_non_blocking")
    )
    return ReviewFindingsResult(
        findings=findings,
        iteration_findings=_snapshot_iteration_findings(
            data["iteration_findings"], "iteration_findings"
        ),
        previous_iteration_findings=_snapshot_iteration_findings(
            data["previous_iteration_findings"], "previous_iteration_findings"
        ),
        open_non_blocking=open_non_blocking,
        processed_comment_ids=_snapshot_string_tuple(
            data["processed_comment_ids"], "processed_comment_ids"
        ),
        ignored_untrusted_comment_count=_snapshot_non_negative_int(
            data["ignored_untrusted_comment_count"], "ignored_untrusted_comment_count"
        ),
        needs_classification_count=needs_classification_count,
    )


def _validate_review_findings_snapshot_action(
    loop_id: str, project_dir: str, action_id: str, lease_token: str
) -> None:
    """Bind snapshot access to the active external-review action and lease."""
    lc._ensure_valid_lease(loop_id, project_dir, lease_token)
    state = lc.load_state(loop_id, project_dir)
    pending = state.pending_action
    if pending is None or pending.action_id != action_id or pending.phase != state.phase:
        raise lc.StaleActionError(f"stale PR review snapshot action: {action_id}")
    if pending.action != lc.Action.WAIT_EXTERNAL_REVIEW.value:
        raise lc.ProtocolViolationError(
            f"action {pending.action} cannot access PR review findings snapshot"
        )


def _load_review_findings_snapshot_artifact(loop_id: str, project_dir: str, action_id: str) -> Any:
    """Load one bounded 0600, regular, non-symlink snapshot JSON artifact."""
    path = lc.artifact_path(loop_id, project_dir, action_id, REVIEW_FINDINGS_SNAPSHOT_ARTIFACT)
    try:
        path_stat = path.lstat()
    except FileNotFoundError as exc:
        raise PrReviewWaitError("review findings snapshot artifact is missing") from exc
    except OSError as exc:
        raise PrReviewWaitError("review findings snapshot artifact is unavailable") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        _raise_invalid_snapshot("artifact must be a regular non-symlink file")
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        _raise_invalid_snapshot("O_NOFOLLOW is unavailable")
    try:
        fd = os.open(path, os.O_RDONLY | no_follow)
    except OSError as exc:
        raise PrReviewWaitError("review findings snapshot artifact is unavailable") from exc
    try:
        opened_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_dev != path_stat.st_dev
            or opened_stat.st_ino != path_stat.st_ino
        ):
            _raise_invalid_snapshot("artifact changed or is not a regular file")
        if stat.S_IMODE(opened_stat.st_mode) != lc.FILE_MODE:
            _raise_invalid_snapshot("artifact mode must be 0600")
        if opened_stat.st_size > MAX_REVIEW_FINDINGS_SNAPSHOT_BYTES:
            _raise_invalid_snapshot("artifact exceeds size limit")
        with os.fdopen(fd, "rb") as file:
            fd = -1
            content = file.read(MAX_REVIEW_FINDINGS_SNAPSHOT_BYTES + 1)
    except PrReviewWaitError:
        raise
    except OSError as exc:
        raise PrReviewWaitError("review findings snapshot artifact is unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if len(content) > MAX_REVIEW_FINDINGS_SNAPSHOT_BYTES:
        _raise_invalid_snapshot("artifact exceeds size limit")
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PrReviewWaitError("invalid review findings snapshot: malformed JSON") from exc


def _snapshot_imported_finding(value: Any, index: int) -> ImportedFinding:
    field_name = f"findings[{index}]"
    data = _snapshot_mapping(
        value,
        {
            "signature",
            "severity",
            "source_comment_id",
            "body_excerpt",
            "path",
            "line",
            "needs_classification",
        },
        field_name,
    )
    severity = data["severity"]
    if not isinstance(severity, str) or severity not in SEVERITIES:
        _raise_invalid_snapshot(f"{field_name}.severity is invalid")
    path = data["path"]
    if path is not None and not isinstance(path, str):
        _raise_invalid_snapshot(f"{field_name}.path must be a string or null")
    line = data["line"]
    if line is not None and (not isinstance(line, int) or isinstance(line, bool)):
        _raise_invalid_snapshot(f"{field_name}.line must be an integer or null")
    needs_classification = data["needs_classification"]
    if not isinstance(needs_classification, bool):
        _raise_invalid_snapshot(f"{field_name}.needs_classification must be a boolean")
    return ImportedFinding(
        signature=_snapshot_string(data["signature"], f"{field_name}.signature"),
        severity=cast(Severity, severity),
        source_comment_id=_snapshot_string(
            data["source_comment_id"], f"{field_name}.source_comment_id"
        ),
        body_excerpt=_snapshot_string(data["body_excerpt"], f"{field_name}.body_excerpt"),
        path=path,
        line=line,
        needs_classification=needs_classification,
    )


def _snapshot_open_non_blocking(value: Any, field_name: str) -> tuple[NonBlockingFinding, ...]:
    if not isinstance(value, list):
        _raise_invalid_snapshot(f"{field_name} must be a list")
    return tuple(
        _snapshot_non_blocking_finding(item, f"{field_name}[{index}]")
        for index, item in enumerate(value)
    )


def _snapshot_non_blocking_finding(value: Any, field_name: str) -> NonBlockingFinding:
    data = _snapshot_mapping(
        value, {"signature", "severity", "path", "line", "body_excerpt"}, field_name
    )
    severity = data["severity"]
    if not isinstance(severity, str) or severity not in {"medium", "low"}:
        _raise_invalid_snapshot(f"{field_name}.severity must be 'medium' or 'low'")
    path = data["path"]
    if path is not None and not isinstance(path, str):
        _raise_invalid_snapshot(f"{field_name}.path must be a string or null")
    line = data["line"]
    if line is not None and (not isinstance(line, int) or isinstance(line, bool)):
        _raise_invalid_snapshot(f"{field_name}.line must be an integer or null")
    return NonBlockingFinding(
        signature=_snapshot_string(data["signature"], f"{field_name}.signature"),
        severity=cast(Severity, severity),
        path=path,
        line=line,
        body_excerpt=_snapshot_string(data["body_excerpt"], f"{field_name}.body_excerpt"),
    )


def _snapshot_iteration_findings(value: Any, field_name: str) -> IterationFindings:
    data = _snapshot_mapping(value, {"signatures", "new_count"}, field_name)
    signatures = _snapshot_string_tuple(data["signatures"], f"{field_name}.signatures")
    if len(signatures) != len(set(signatures)):
        _raise_invalid_snapshot(f"{field_name}.signatures contains duplicates")
    new_count = _snapshot_non_negative_int(data["new_count"], f"{field_name}.new_count")
    if new_count > len(signatures):
        _raise_invalid_snapshot(f"{field_name}.new_count exceeds signatures")
    return IterationFindings(frozenset(signatures), new_count)


def _snapshot_mapping(value: Any, expected_keys: set[str], field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected_keys:
        _raise_invalid_snapshot(f"{field_name} has invalid fields")
    return value


def _snapshot_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _raise_invalid_snapshot(f"{field_name} must be a list of strings")
    return tuple(value)


def _snapshot_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        _raise_invalid_snapshot(f"{field_name} must be a string")
    return value


def _snapshot_non_negative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _raise_invalid_snapshot(f"{field_name} must be a non-negative integer")
    return value


def _raise_invalid_snapshot(reason: str) -> None:
    raise PrReviewWaitError(f"invalid review findings snapshot: {reason}")


def _upsert_finding(
    findings_map: dict[str, dict[str, Any]], finding: ImportedFinding, iteration: int
) -> None:
    existing = findings_map.get(finding.signature)
    if isinstance(existing, dict):
        source_ids = sorted(
            set(existing.get("source_comment_ids") or []) | {finding.source_comment_id}
        )
        pending_ids = set(existing.get("pending_classification_source_comment_ids") or [])
        confirmed_severity = _confirmed_severity(existing)
        if finding.needs_classification:
            pending_ids.add(finding.source_comment_id)
        else:
            confirmed_severity = _highest_optional_severity(confirmed_severity, finding.severity)
        # A signature previously marked "addressed" (mark_addressed_findings, #235) that shows
        # up again here has, by definition, reraised: build_pr_iteration_findings() already
        # treats it as blocking again via last_seen_iteration regardless of the stored `status`,
        # but leaving `status` at "addressed" left the exit-comment matrix
        # (_format_pr_review_findings_matrix) misreporting a currently-blocking finding as
        # resolved, and left mark_addressed_findings()'s `status == "open"` guard permanently
        # skipping the record on a later genuine fix (its addressed_at_commit/iteration would
        # then still point at the stale, insufficient commit). Reopening here -- and clearing
        # the stale addressed_at_* markers -- keeps both in sync with the reraise.
        reopened = existing.get("status") == "addressed"
        merged = {
            **existing,
            "last_seen_iteration": (
                existing.get("last_seen_iteration") if finding.needs_classification else iteration
            ),
            "severity": "high" if pending_ids else confirmed_severity,
            "confirmed_severity": confirmed_severity,
            "pending_classification_source_comment_ids": sorted(pending_ids),
            "source_comment_ids": source_ids,
            # Refreshed to the latest re-raise's location/text (display-only, not part of the
            # signature identity) so a stale early comment doesn't linger in reports (#213).
            "path": finding.path,
            "line": finding.line,
            "body_excerpt": finding.body_excerpt,
        }
        if reopened:
            merged["status"] = "open"
            merged["addressed_at_commit"] = None
            merged["addressed_at_iteration"] = None
            # PR #276 review (high): `resolved_thread_ids` recorded which GitHub threads
            # `resolve_addressed_findings` already resolved for the *stale* addressed_at_*
            # this reraise just cleared. Leaving them stale would make a later genuine
            # re-fix's `_resolve_addressed_signature_threads` treat those thread ids as
            # "already_resolved" and skip reply/resolve even for a thread GitHub has since
            # reopened, so this reraise must clear them in lockstep with addressed_at_*.
            merged["resolved_thread_ids"] = []
        findings_map[finding.signature] = merged
        return
    source_ids = [finding.source_comment_id]
    pending_ids = [finding.source_comment_id] if finding.needs_classification else []
    confirmed_severity = None if finding.needs_classification else finding.severity
    findings_map[finding.signature] = {
        "first_seen_iteration": iteration,
        "last_seen_iteration": iteration,
        "status": "open",
        "severity": "high" if pending_ids else confirmed_severity,
        "confirmed_severity": confirmed_severity,
        "pending_classification_source_comment_ids": pending_ids,
        "dismiss_reason": None,
        "source_comment_ids": source_ids,
        "path": finding.path,
        "line": finding.line,
        "body_excerpt": finding.body_excerpt,
    }


def _apply_classification_to_state(
    findings_map: dict[str, dict[str, Any]],
    finding: ImportedFinding,
    decision: SeverityDecision,
    iteration: int,
) -> None:
    record = findings_map.get(finding.signature)
    if not isinstance(record, dict):
        raise PrReviewWaitError(f"missing finding state for classification: {finding.signature}")
    source_ids = set(record.get("source_comment_ids") or [])
    pending_ids = set(record.get("pending_classification_source_comment_ids") or [])
    if finding.source_comment_id not in source_ids:
        raise PrReviewWaitError(
            f"finding source is missing from state: {finding.source_comment_id}"
        )
    pending_ids.discard(finding.source_comment_id)
    confirmed_severity = _confirmed_severity(record)
    if decision.severity is None:
        source_ids.discard(finding.source_comment_id)
    else:
        confirmed_severity = _highest_optional_severity(confirmed_severity, decision.severity)
    if confirmed_severity is None and not pending_ids:
        del findings_map[finding.signature]
        return
    findings_map[finding.signature] = {
        **record,
        "last_seen_iteration": (
            iteration if decision.severity is not None else record.get("last_seen_iteration")
        ),
        "severity": "high" if pending_ids else confirmed_severity,
        "confirmed_severity": confirmed_severity,
        "pending_classification_source_comment_ids": sorted(pending_ids),
        "source_comment_ids": sorted(source_ids),
    }


def _confirmed_severity(record: dict[str, Any]) -> Severity | None:
    if "confirmed_severity" in record:
        value = record.get("confirmed_severity")
        return value if value in SEVERITIES else None  # type: ignore[return-value]
    value = record.get("severity")
    return value if value in SEVERITIES else None  # type: ignore[return-value]


def _highest_optional_severity(existing: Severity | None, incoming: Severity) -> Severity:
    if existing is None:
        return incoming
    return max((existing, incoming), key=lambda item: SEVERITY_ORDER[item])


def _journal_ignored_untrusted(
    loop_id: str, project_dir: str, action_id: str | None, item: ReviewItem
) -> None:
    user = item.raw.get("user") if isinstance(item.raw.get("user"), dict) else {}
    lc.append_journal_event(
        loop_id,
        project_dir,
        "ignored_untrusted_comment",
        "waiter",
        action_id,
        {
            "source": item.source,
            "comment_id": item.item_id,
            "login": user.get("login"),
            "author_association": item.raw.get("author_association"),
            "body_excerpt": _redacted_excerpt(item.body),
            "notification_required": True,
        },
    )


def _journal_ignored_untrusted_review(
    loop_id: str, project_dir: str, action_id: str | None, item: IgnoredUntrustedReview
) -> None:
    lc.append_journal_event(
        loop_id,
        project_dir,
        "ignored_untrusted_comment",
        "waiter",
        action_id,
        {
            "source": "review",
            "comment_id": str(item.review_id),
            "login": item.login,
            "author_association": item.author_association,
            "body_excerpt": item.body_excerpt,
            "notification_required": True,
        },
    )


def _ignored_review_key(item: IgnoredUntrustedReview) -> str:
    return f"review:{item.review_id}"


def _is_importable(
    item: ReviewItem, baseline: dict[str, Any], processed_comment_ids: set[str]
) -> bool:
    if _comment_key(item) in processed_comment_ids:
        return False
    if not _is_after_baseline_time(item, str(baseline["baseline_recorded_at"])):
        return False
    if item.source == "review":
        return _int_or_zero(item.item_id) > _int_or_zero(baseline["baseline_review_id"])
    if item.source == "review_comment" and item.pull_request_review_id is not None:
        return item.pull_request_review_id > _int_or_zero(baseline["baseline_review_id"])
    return True


def _is_after_baseline_time(item: ReviewItem, baseline_recorded_at: str) -> bool:
    item_time = _parse_datetime(item.created_at)
    baseline_time = _parse_datetime(baseline_recorded_at)
    if item_time is None or baseline_time is None:
        return False
    return _truncate_to_second(item_time) >= _truncate_to_second(baseline_time)


def _truncate_to_second(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def _baseline_from_state(pr_review: dict[str, Any]) -> dict[str, Any]:
    if "baseline_review_id" not in pr_review or "baseline_recorded_at" not in pr_review:
        raise PrReviewWaitError("pr_review baseline is missing")
    return {
        "baseline_review_id": _int_or_zero(pr_review["baseline_review_id"]),
        "baseline_recorded_at": str(pr_review["baseline_recorded_at"]),
        "iteration_head_sha": pr_review.get("iteration_head_sha"),
    }


def _ensure_pr_review_state(value: dict[str, Any] | None) -> dict[str, Any]:
    pr_review = copy.deepcopy(value) if isinstance(value, dict) else {}
    pr_review.setdefault("processed_comment_ids", [])
    pr_review.setdefault("findings", {})
    return pr_review


def _processed_comment_ids(pr_review: dict[str, Any]) -> list[str]:
    values = pr_review.get("processed_comment_ids")
    if not isinstance(values, list):
        return []
    return [str(item) for item in values]


def _findings_map(pr_review: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values = pr_review.get("findings")
    if not isinstance(values, dict):
        return {}
    return {
        str(key): dict(value)
        for key, value in values.items()
        if isinstance(value, dict) and isinstance(key, str)
    }


def _open_non_blocking_findings(
    findings_map: dict[str, dict[str, Any]],
) -> tuple[NonBlockingFinding, ...]:
    """Collect all currently open (non-dismissed), medium/low findings, sorted by signature.

    Cumulative across every iteration recorded in `findings_map` (issue #213) -- not just the
    findings imported by the current action -- so a `pr_review_response` exit surfaces every
    non-blocking finding nobody dismissed, even ones re-raised many iterations ago.
    """
    items: list[NonBlockingFinding] = []
    for signature in sorted(findings_map):
        record = findings_map[signature]
        if record.get("status") == "dismissed":
            continue
        severity = record.get("severity")
        if severity not in {"medium", "low"}:
            continue
        path = record.get("path")
        line = record.get("line")
        # PR#228 review: normalize whitespace (collapse newlines/runs of spaces) *before*
        # truncating, so a multi-line reviewer comment can never break a consumer's
        # single-line bullet rendering (e.g. `loop_driver._exit_success_comment()`) and so the
        # 200-char limit applies to the normalized text a reader actually sees, not to
        # whatever's left after an arbitrary mid-newline cut.
        normalized_excerpt = " ".join(str(record.get("body_excerpt") or "").split())
        items.append(
            NonBlockingFinding(
                signature=signature,
                severity=cast(Severity, severity),
                path=path if isinstance(path, str) else None,
                line=line if isinstance(line, int) and not isinstance(line, bool) else None,
                body_excerpt=normalized_excerpt[:NON_BLOCKING_EXCERPT_LIMIT],
            )
        )
    return tuple(items)


def _parse_reviewer_allowlist(value: Any) -> tuple[ReviewerAllowlistEntry, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(
            "pr_review.reviewer_allowlist is required. Confirm the actual GitHub bot "
            "login/App slug for the Codex connector and set it in "
            ".claude/config/loop-harness/loop-harness.local.yaml."
        )
    entries: list[ReviewerAllowlistEntry] = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError("pr_review.reviewer_allowlist entries must be mappings")
        app_slug = _optional_str(item.get("app_slug"))
        login = _optional_str(item.get("login"))
        if not app_slug and not login:
            raise ConfigError("reviewer_allowlist entry requires app_slug or login")
        entries.append(
            ReviewerAllowlistEntry(
                app_slug=app_slug,
                login=login,
                user_type=_optional_str(item.get("type")) or "Bot",
                author_associations=_string_set(item.get("author_association")),
            )
        )
    return tuple(entries)


def _parse_dedup_config(value: Any) -> DedupConfig:
    dedup = value if isinstance(value, dict) else {}
    return DedupConfig(
        line_bucket_size=_positive_int(dedup.get("line_bucket_size"), DEFAULT_LINE_BUCKET_SIZE),
        stopwords_en=DEFAULT_STOPWORDS_EN | _string_set(dedup.get("stopwords_en")),
        stopwords_ja=DEFAULT_STOPWORDS_JA | _string_set(dedup.get("stopwords_ja")),
        signature_footer_patterns=tuple(
            _string_list(dedup.get("signature_footer_patterns")) or DEFAULT_FOOTER_PATTERNS
        ),
    )


def _parse_auto_generated_markers(value: Any) -> tuple[str, ...]:
    """Parse pr_review.auto_generated_markers: absent means default, explicit list overrides."""
    if value is None:
        return DEFAULT_AUTO_GENERATED_MARKERS
    return tuple(_string_list(value))


def _parse_severity_markers(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        return ()
    markers: list[tuple[str, str]] = []
    for key, raw in value.items():
        if str(key) in SEVERITIES:
            for pattern in _string_list(raw):
                markers.append(_validated_severity_marker(str(pattern), str(key)))
            continue
        if str(raw) in SEVERITIES:
            markers.append(_validated_severity_marker(str(key), str(raw)))
    return tuple(markers)


def _validated_severity_marker(pattern: str, severity: str) -> tuple[str, str]:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ConfigError(
            f"invalid pr_review.severity_markers regex for {severity}: {pattern}"
        ) from exc
    return pattern, severity


def _explicit_severity(body: str, config: PrReviewConfig) -> tuple[Severity, str] | None:
    matches: list[tuple[str, str]] = []
    for pattern, severity, reason in _default_severity_patterns():
        if re.search(pattern, body or "", re.IGNORECASE):
            matches.append((severity, reason))
    for pattern, severity in config.severity_markers:
        if re.search(pattern, body or "", re.IGNORECASE):
            matches.append((severity, "custom_marker"))
    if not matches:
        return None
    severity, reason = max(matches, key=lambda item: SEVERITY_ORDER[item[0]])
    return severity, reason


def _default_severity_patterns() -> tuple[tuple[str, str, str], ...]:
    return (
        (r"\bP1\b", "critical", "priority_marker"),
        (r"\[critical\]|\bCRITICAL\b|\U0001F534", "critical", "severity_marker"),
        (r"\bP2\b", "high", "priority_marker"),
        (r"\[high\]|\bHIGH\b", "high", "severity_marker"),
        (r"\[must\]|MUST\s*FIX|\bblocking\b", "high", "must_fix_marker"),
        (r"\bP3\b", "medium", "priority_marker"),
        (r"\[medium\]|\bMEDIUM\b", "medium", "severity_marker"),
        (r"\bP4\b", "low", "priority_marker"),
        (r"\[low\]|\bLOW\b|\[nit\]|\bNIT\b|nitpick", "low", "severity_marker"),
    )


def _parse_classification_response(
    value: str,
) -> tuple[Severity | Literal["none"], Literal["high", "low"]] | None:
    severity_match = re.search(
        r"^SEVERITY:\s*(critical|high|medium|low|none)\s*$", value, re.I | re.M
    )
    confidence_match = re.search(r"^CONFIDENCE:\s*(high|low)\s*$", value, re.I | re.M)
    if severity_match is None or confidence_match is None:
        return None
    severity = severity_match.group(1).lower()
    confidence = confidence_match.group(1).lower()
    return severity, confidence  # type: ignore[return-value]


def _review_item_from_review(raw: dict[str, Any]) -> ReviewItem:
    return ReviewItem(
        source="review",
        item_id=str(raw.get("id") or ""),
        body=str(raw.get("body") or ""),
        created_at=_optional_str(raw.get("submitted_at")),
        path=None,
        line=None,
        original_line=None,
        pull_request_review_id=_int_or_none(raw.get("id")),
        raw=raw,
    )


def _review_item_from_review_comment(raw: dict[str, Any]) -> ReviewItem:
    return ReviewItem(
        source="review_comment",
        item_id=str(raw.get("id") or ""),
        body=str(raw.get("body") or ""),
        created_at=_optional_str(raw.get("created_at")),
        path=_optional_str(raw.get("path")),
        line=_int_or_none(raw.get("line")),
        original_line=_int_or_none(raw.get("original_line")),
        pull_request_review_id=_int_or_none(raw.get("pull_request_review_id")),
        raw=raw,
    )


def _review_item_from_issue_comment(raw: dict[str, Any]) -> ReviewItem:
    return ReviewItem(
        source="issue_comment",
        item_id=str(raw.get("id") or ""),
        body=str(raw.get("body") or ""),
        created_at=_optional_str(raw.get("created_at")),
        path=None,
        line=None,
        original_line=None,
        pull_request_review_id=None,
        raw=raw,
    )


def _comment_key(item: ReviewItem) -> str:
    if item.source not in REVIEW_SOURCES:
        raise ValueError(f"unsupported review source: {item.source}")
    return f"{item.source}:{item.item_id}"


def _entry_allows_app(entry: ReviewerAllowlistEntry, app_slug: str, raw: dict[str, Any]) -> bool:
    return entry.app_slug == app_slug and _entry_allows_association(entry, raw)


def _entry_allows_login(
    entry: ReviewerAllowlistEntry, login: Any, user_type: Any, raw: dict[str, Any]
) -> bool:
    return (
        entry.login == login
        and user_type == entry.user_type
        and _entry_allows_association(entry, raw)
    )


def _entry_allows_association(entry: ReviewerAllowlistEntry, raw: dict[str, Any]) -> bool:
    if not entry.author_associations:
        return True
    return raw.get("author_association") in entry.author_associations


def _github_app_slug(raw: dict[str, Any]) -> str | None:
    app = raw.get("performed_via_github_app")
    if not isinstance(app, dict):
        return None
    return _optional_str(app.get("slug"))


def _list_payload(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _int_or_zero(value: Any) -> int:
    parsed = _int_or_none(value)
    return parsed if parsed is not None else 0


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


def _string_set(value: Any) -> frozenset[str]:
    return frozenset(_string_list(value))


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _redacted_excerpt(value: str) -> str:
    text = lc.redact(value).replace("\n", " ").strip()
    return text[:EXCERPT_LIMIT]
