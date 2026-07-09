#!/usr/bin/env python3
"""Deterministic PR review wait/import helpers for loop-harness."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

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
REVIEW_SOURCES = frozenset({"review", "review_comment", "issue_comment"})
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
SEVERITIES = frozenset(SEVERITY_ORDER)
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
class CompletionOutcome:
    """Result of polling for external review completion."""

    signal: Literal["review_submitted", "check_run_completed", "timeout", "api_error", "pending"]
    completed: bool
    timed_out: bool
    infrastructure_failure: bool
    review_ids: tuple[int, ...] = ()
    check_run_names: tuple[str, ...] = ()
    ignored_untrusted_review_count: int = 0
    error: str | None = None


@dataclass(frozen=True)
class SeverityDecision:
    """Deterministic severity classification result."""

    severity: Literal["critical", "high", "medium", "low"]
    source: Literal["explicit", "external_classification", "fail_safe"]
    needs_classification: bool
    reason: str


@dataclass(frozen=True)
class ImportedFinding:
    """Trusted imported PR review finding."""

    signature: str
    severity: Literal["critical", "high", "medium", "low"]
    source_comment_id: str
    body_excerpt: str
    path: str | None
    line: int | None
    needs_classification: bool


IterationFindings = lc.IterationFindings
NoProgressResult = lc.NoProgressResult


@dataclass(frozen=True)
class ReviewFindingsResult:
    """Result of importing trusted review findings."""

    findings: tuple[ImportedFinding, ...]
    iteration_findings: IterationFindings
    previous_iteration_findings: IterationFindings
    processed_comment_ids: tuple[str, ...]
    ignored_untrusted_comment_count: int
    needs_classification_count: int


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
    )


def record_baseline(
    loop_id: str,
    project_dir: str,
    pr_number: int,
    client: GhApiClient,
    lease_token: str,
    *,
    action_id: str | None = None,
) -> BaselineRecord:
    """Record review/comment baseline before push or PR creation."""
    recorded_at = lc.now_iso()
    reviews = _fetch_reviews(client, pr_number)
    review_comments = _fetch_review_comments(client, pr_number)
    issue_comments = _fetch_issue_comments(client, pr_number)
    baseline_review_id = max([_int_or_zero(item.get("id")) for item in reviews] or [0])
    processed_ids = {
        *(_comment_key(_review_item_from_review(item)) for item in reviews),
        *(_comment_key(_review_item_from_review_comment(item)) for item in review_comments),
        *(_comment_key(_review_item_from_issue_comment(item)) for item in issue_comments),
    }
    state = lc.load_state(loop_id, project_dir)
    pr_review = _ensure_pr_review_state(state.pr_review)
    existing = set(_processed_comment_ids(pr_review))
    pr_review["baseline_review_id"] = baseline_review_id
    pr_review["baseline_recorded_at"] = recorded_at
    pr_review["processed_comment_ids"] = sorted(existing | processed_ids)
    state.pr_review = pr_review
    state.state_version += 1
    state.updated_at = lc.now_iso()
    lc._ensure_valid_lease(loop_id, project_dir, lease_token)
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
) -> str:
    """Record the post-push PR head SHA for check-run fallback scoping."""
    payload = client.api(f"repos/{client.repo}/pulls/{pr_number}")
    head = payload.get("head") if isinstance(payload, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not sha:
        raise GitHubApiError("pull request head.sha is missing")
    state = lc.load_state(loop_id, project_dir)
    pr_review = _ensure_pr_review_state(state.pr_review)
    pr_review["iteration_head_sha"] = sha
    state.pr_review = pr_review
    state.state_version += 1
    state.updated_at = lc.now_iso()
    lc._ensure_valid_lease(loop_id, project_dir, lease_token)
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
    ignored_keys: set[str] = set()
    while True:
        try:
            review_outcome = _review_completion_outcome(
                pr_number, baseline, config, client, ignored_keys
            )
            if review_outcome.completed:
                return review_outcome
            check_outcome = _checkrun_completion_outcome(baseline, config, client)
            if check_outcome.completed:
                return check_outcome
        except GitHubApiError as exc:
            return CompletionOutcome(
                "api_error",
                completed=False,
                timed_out=False,
                infrastructure_failure=True,
                error=str(exc),
            )
        if monotonic() - start >= config.timeout_seconds:
            return CompletionOutcome(
                "timeout",
                completed=False,
                timed_out=True,
                infrastructure_failure=False,
                ignored_untrusted_review_count=len(ignored_keys),
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
) -> ReviewFindingsResult:
    """Import trusted post-baseline review findings and update loop state."""
    state = lc.load_state(loop_id, project_dir)
    pr_review = _ensure_pr_review_state(state.pr_review)
    baseline = _baseline_from_state(pr_review)
    processed = set(_processed_comment_ids(pr_review))
    findings_map = _findings_map(pr_review)
    previous_iteration_findings = lc.build_pr_iteration_findings(pr_review, iteration - 1)
    imported: list[ImportedFinding] = []
    ignored_items: list[ReviewItem] = []

    for item in _fetch_all_review_items(client, pr_number):
        key = _comment_key(item)
        if not _is_importable(item, baseline, processed):
            continue
        processed.add(key)
        if not verify_origin(item.raw, config.reviewer_allowlist):
            ignored_items.append(item)
            continue
        finding = _finding_from_item(item, key, config, iteration)
        if finding is None:
            continue
        imported.append(finding)
        _upsert_finding(findings_map, finding, iteration)

    pr_review["processed_comment_ids"] = sorted(processed)
    pr_review["findings"] = findings_map
    state.pr_review = pr_review
    state.ignored_untrusted_comment_count += len(ignored_items)
    state.state_version += 1
    state.updated_at = lc.now_iso()
    iteration_findings = build_iteration_findings(pr_review, iteration)
    lc._ensure_valid_lease(loop_id, project_dir, lease_token)
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
            "signatures": sorted(iteration_findings.signatures),
            "new_count": iteration_findings.new_count,
        },
    )
    lc._write_state(state, project_dir)
    return ReviewFindingsResult(
        findings=tuple(imported),
        iteration_findings=iteration_findings,
        previous_iteration_findings=previous_iteration_findings,
        processed_comment_ids=tuple(pr_review["processed_comment_ids"]),
        ignored_untrusted_comment_count=len(ignored_items),
        needs_classification_count=sum(1 for item in imported if item.needs_classification),
    )


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
    return SeverityDecision(severity, "external_classification", False, "classified")


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
    state.state_version += 1
    state.updated_at = lc.now_iso()
    lc._ensure_valid_lease(loop_id, project_dir, lease_token)
    lc.append_journal_event(
        loop_id,
        project_dir,
        "dismissed",
        decided_by,
        action_id,
        {"signature": signature, "reason": reason, "decided_by": decided_by},
    )
    lc._write_state(state, project_dir)


def phase_check_from_review_findings(result: ReviewFindingsResult) -> lc.PhaseCheckResult:
    """Convert imported PR review findings into the shared checker result shape."""
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
    critical_high = [item for item in findings if item.severity in {"critical", "high"}]
    signature = lc.compute_pr_review_signature(list(result.iteration_findings.signatures))
    check = lc.CheckResult(
        passed=not critical_high,
        layer="llm_review",
        signature=signature,
        findings=findings,
        raw_artifact_path="",
    )
    return lc.PhaseCheckResult(
        passed=not critical_high,
        results=[check],
        signature=signature,
        infrastructure_failure=False,
        metadata={
            "previous_iteration_findings": _iteration_findings_dict(
                result.previous_iteration_findings
            ),
            "current_iteration_findings": _iteration_findings_dict(result.iteration_findings),
        },
    )


def phase_check_from_completion_outcome(outcome: CompletionOutcome) -> lc.PhaseCheckResult:
    """Represent timeout/API polling outcomes as checker-compatible results."""
    if outcome.infrastructure_failure:
        return lc.PhaseCheckResult(False, [], "pr_review_api_error", True)
    if outcome.timed_out:
        return lc.PhaseCheckResult(False, [], "pr_review_timeout", False)
    return lc.PhaseCheckResult(True, [], "", False)


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
    ignored_keys: set[str],
) -> CompletionOutcome:
    baseline_id = _int_or_zero(baseline.get("baseline_review_id"))
    reviews = _fetch_reviews(client, pr_number)
    trusted_ids: list[int] = []
    for item in reviews:
        review_id = _int_or_zero(item.get("id"))
        if review_id <= baseline_id:
            continue
        if verify_origin(item, config.reviewer_allowlist):
            trusted_ids.append(review_id)
            continue
        ignored_keys.add(f"review:{review_id}")
    if not trusted_ids:
        return CompletionOutcome(
            "pending",
            completed=False,
            timed_out=False,
            infrastructure_failure=False,
            ignored_untrusted_review_count=len(ignored_keys),
        )
    return CompletionOutcome(
        "review_submitted",
        completed=True,
        timed_out=False,
        infrastructure_failure=False,
        review_ids=tuple(sorted(trusted_ids)),
        ignored_untrusted_review_count=len(ignored_keys),
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


def _finding_from_item(
    item: ReviewItem, key: str, config: PrReviewConfig, iteration: int
) -> ImportedFinding | None:
    if not item.body.strip():
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


def _upsert_finding(
    findings_map: dict[str, dict[str, Any]], finding: ImportedFinding, iteration: int
) -> None:
    existing = findings_map.get(finding.signature)
    if isinstance(existing, dict):
        source_ids = sorted(
            set(existing.get("source_comment_ids") or []) | {finding.source_comment_id}
        )
        findings_map[finding.signature] = {
            **existing,
            "last_seen_iteration": iteration,
            "source_comment_ids": source_ids,
        }
        return
    source_ids = [finding.source_comment_id]
    findings_map[finding.signature] = {
        "first_seen_iteration": iteration,
        "last_seen_iteration": iteration,
        "status": "open",
        "severity": finding.severity,
        "dismiss_reason": None,
        "source_comment_ids": source_ids,
    }


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
    return item_time > baseline_time


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


def _parse_severity_markers(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        return ()
    markers: list[tuple[str, str]] = []
    for key, raw in value.items():
        if str(key) in SEVERITIES:
            markers.extend((str(pattern), str(key)) for pattern in _string_list(raw))
            continue
        if str(raw) in SEVERITIES:
            markers.append((str(key), str(raw)))
    return tuple(markers)


def _explicit_severity(
    body: str, config: PrReviewConfig
) -> tuple[Literal["critical", "high", "medium", "low"], str] | None:
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
) -> tuple[Literal["critical", "high", "medium", "low"], Literal["high", "low"]] | None:
    severity_match = re.search(r"^SEVERITY:\s*(critical|high|medium|low)\s*$", value, re.I | re.M)
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
