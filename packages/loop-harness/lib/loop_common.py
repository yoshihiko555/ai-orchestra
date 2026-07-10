#!/usr/bin/env python3
"""Deterministic core for loop-harness state, guards, locks, and journals."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = 1
FILE_MODE = 0o600
DIR_MODE = 0o700
ACTION_ID_BYTES = 6
HASH_LENGTH = 16
GIT_TIMEOUT_SECONDS = 5
_ROOT_CACHE: dict[str, Path] = {}
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_RESERVED_PROPOSAL_CONTEXT_KEYS = frozenset({"lease_token", "params", "reason"})
_PUBLIC_STOP_REASONS = frozenset(
    {"safety_stop", "push_guard_violation", "repo_identity_mismatch", "foreign_live_lease"}
)

DEFAULT_CONFIG: dict[str, Any] = {
    "guards": {
        "max_iterations": 3,
        "no_progress": {"repeat": 2},
        "infrastructure_failure": {"max_retries": 3},
    },
    "lock": {
        "ttl_seconds": {"lp1": 3600, "lp2": 300},
        "heartbeat_interval_seconds": 60,
    },
}

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(
        r"\b[A-Za-z0-9_-]{0,20}(api[_-]?key|token|password|secret|credential)\b\s*[:=]\s*"
        r"(\"[^\"]*\"|'[^']*'|[^,;\n]+)",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\b(AKIA|ASIA|A3T)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
    re.compile(r"\bSharedAccessSignature\s*=\s*\S+", re.IGNORECASE),
    re.compile(
        r"-----BEGIN ((?:RSA|OPENSSH|EC|DSA) PRIVATE KEY|PRIVATE KEY)-----"
        r".*?-----END \1-----",
        re.DOTALL,
    ),
]
_SENSITIVE_KEY_RE = re.compile(r"(api[_-]?key|token|password|secret|credential)", re.IGNORECASE)

_FAILED_NODEID_RE = re.compile(r"^FAILED\s+(\S+?)(?:\s+-\s+.*)?$", re.MULTILINE)
_RUFF_RULE_RE = re.compile(r"^\S+:\d+:\d+:\s+([A-Z]{1,4}\d{2,4})\b", re.MULTILINE)
_STRICT_CHECKER_LAYERS = frozenset({"mechanical", "llm_review"})
_STRICT_PHASE_CHECK_KEYS = frozenset(
    {"passed", "results", "signature", "infrastructure_failure", "metadata"}
)
CHECK_RESULT_KEYS = frozenset(
    {
        "passed",
        "layer",
        "signature",
        "findings",
        "raw_artifact_path",
        "infrastructure_failure",
    }
)
FINDING_KEYS = frozenset({"severity", "summary", "source", "path", "line"})


class Action(StrEnum):
    """Shared action vocabulary imported by later loop-harness phases."""

    RUN_MAKER = "run_maker"
    RUN_CHECKER = "run_checker"
    WAIT_EXTERNAL_REVIEW = "wait_external_review"
    ADVANCE_PHASE = "advance_phase"
    STOP = "stop"
    EXIT_SUCCESS = "exit_success"
    EXIT_FAILURE = "exit_failure"


class LoopHarnessError(RuntimeError):
    """Base error for loop-harness core failures."""


class StaleActionError(LoopHarnessError):
    """Raised when complete receives a stale action_id or state_version."""


class ProtocolViolationError(LoopHarnessError):
    """Raised when the caller violates the two-phase protocol."""


class WriteRejectedError(LoopHarnessError):
    """Raised when lease fencing rejects a state/journal mutation."""


class LockNotFoundError(LoopHarnessError):
    """Raised when attach cannot find an existing lock."""


class ForeignLeaseError(LoopHarnessError):
    """Raised when another live lease prevents ownership."""

    def __init__(self, lock: LockInfo | None) -> None:
        super().__init__("live foreign lease")
        self.lock = lock


class IntegrityError(LoopHarnessError):
    """Raised when state and journal consistency checks fail."""


class InvalidStateError(LoopHarnessError):
    """Raised when an entry point is called for an invalid status."""


class RootResolutionError(LoopHarnessError):
    """Raised when the root worktree cannot be resolved."""


@dataclass
class GuardCounters:
    """Per-phase guard counters."""

    iteration: int = 0
    no_progress_streak: int = 0
    last_signature: str | None = None
    infrastructure_failure_count: int = 0


@dataclass
class PendingAction:
    """Action proposed by propose and waiting for complete."""

    action_id: str
    action: str
    phase: str
    iteration: int
    issued_at: str


@dataclass
class LastCompletedAction:
    """Last complete result for idempotent replay."""

    action_id: str
    state_version_before: int
    state_version_after: int
    result_digest: str
    completed_at: str


@dataclass
class LoopState:
    """state.json schema."""

    schema_version: int
    loop_id: str
    definition_id: str
    repo_identity_hash: str
    phase: str
    iteration: int
    status: str
    worktree_path: str
    branch: str
    pr_number: int | None
    guards: dict[str, GuardCounters]
    last_check_result: dict[str, Any] | None
    pending_action: PendingAction | None
    last_completed_action: LastCompletedAction | None
    stop_reason: str | None
    pr_review: dict[str, Any] | None
    ignored_untrusted_comment_count: int
    created_at: str
    updated_at: str
    state_version: int


@dataclass(frozen=True)
class Finding:
    """LLM or mechanical finding."""

    severity: Literal["critical", "high", "medium", "low"]
    summary: str
    source: str
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class CheckResult:
    """Single checker layer result."""

    passed: bool
    layer: Literal["mechanical", "llm_review"]
    signature: str | None
    findings: list[Finding]
    raw_artifact_path: str
    infrastructure_failure: bool = False


@dataclass(frozen=True)
class PhaseCheckResult:
    """Aggregated phase checker result."""

    passed: bool
    results: list[CheckResult]
    signature: str
    infrastructure_failure: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IterationFindings:
    """PR review finding summary for one iteration."""

    signatures: frozenset[str]
    new_count: int


@dataclass(frozen=True)
class NoProgressResult:
    """PR review no-progress decision."""

    no_progress: bool
    reason: Literal["reraised", "new_count_non_decreasing", "progress"]
    reraised_signatures: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class MechanicalFailure:
    """Normalized mechanical command failure."""

    command: str
    failure_type: str
    error_type: str
    output: str


@dataclass(frozen=True)
class GuardDecision:
    """Guard evaluation result."""

    disposition: str
    reason: str = ""
    next_phase: str | None = None
    next_action: str | None = None


@dataclass(frozen=True)
class ProposeResult:
    """Result returned by propose/start/attach."""

    action: str
    action_id: str
    state_version: int
    expected_phase: str
    phase: str
    iteration: int
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompleteResult:
    """Result returned by complete."""

    ok: bool
    idempotent_replay: bool
    state_version: int
    next_hint: str


@dataclass(frozen=True)
class ReconcileOutcome:
    """Result returned by reconcile."""

    action_taken: str
    state_version: int


@dataclass(frozen=True)
class ResumeResult:
    """Result returned by resume."""

    state: LoopState
    lease_token: str


@dataclass(frozen=True)
class LockInfo:
    """lock.json schema."""

    owner_id: str
    pid: int
    host: str
    started_at: str
    heartbeat_at: str
    ttl: int
    lease_token: str


def now_iso() -> str:
    """Return the current UTC timestamp in ISO8601 format."""
    return datetime.now(tz=UTC).isoformat()


def redact(text: str) -> str:
    """Mask known secret patterns."""
    result = text
    for pattern in SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def redact_payload(value: Any) -> Any:
    """Recursively redact strings in a JSON-like payload."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _is_sensitive_key(key) else redact_payload(item)
            for key, item in value.items()
        }
    return value


def _is_sensitive_key(key: Any) -> bool:
    """Return True when a payload key name indicates a secret value."""
    return bool(_SENSITIVE_KEY_RE.search(str(key)))


def loop_root(project_dir: str) -> Path:
    """Return root-worktree-side .claude/loop path."""
    return resolve_root_worktree(project_dir) / ".claude" / "loop"


def loop_dir(loop_id: str, project_dir: str) -> Path:
    """Return the per-loop state directory."""
    _validate_safe_id("loop_id", loop_id)
    return loop_root(project_dir) / loop_id


def state_path(loop_id: str, project_dir: str) -> Path:
    """Return state.json path."""
    return loop_dir(loop_id, project_dir) / "state.json"


def journal_path(loop_id: str, project_dir: str) -> Path:
    """Return journal.jsonl path."""
    return loop_dir(loop_id, project_dir) / "journal.jsonl"


def lock_path(loop_id: str, project_dir: str) -> Path:
    """Return lock.json path."""
    return loop_dir(loop_id, project_dir) / "lock.json"


def artifact_path(loop_id: str, project_dir: str, action_id: str, name: str) -> Path:
    """Return a safe artifact path."""
    _validate_safe_id("loop_id", loop_id)
    _validate_safe_id("action_id", action_id)
    if Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError(f"Unsafe artifact name: {name}")
    return loop_dir(loop_id, project_dir) / "artifacts" / action_id / name


def load_state(loop_id: str, project_dir: str) -> LoopState:
    """Read state.json."""
    data = _read_json_file(state_path(loop_id, project_dir))
    if not data:
        raise InvalidStateError(f"state not found: {loop_id}")
    return _state_from_dict(data)


def start(
    loop_id: str,
    project_dir: str,
    definition_id: str,
    repo_identity_hash: str,
    worktree_path: str,
    branch: str,
    owner_id: str,
    ttl_seconds: int,
    phase: str = "implementation",
    host: str | None = None,
    preacquired_lock: LockInfo | None = None,
) -> ProposeResult:
    """Create initial state, acquire a lease, and return the first proposal."""
    if state_path(loop_id, project_dir).exists():
        raise InvalidStateError(f"state already exists: {loop_id}; use attach or resume")
    if preacquired_lock is None:
        lock = acquire_lock(loop_id, project_dir, owner_id, ttl_seconds, host)
        if lock is None:
            raise ForeignLeaseError(None)
    else:
        lock = preacquired_lock
        _ensure_valid_lease(loop_id, project_dir, lock.lease_token)
    state = _initial_state(loop_id, definition_id, repo_identity_hash, worktree_path, branch, phase)
    append_journal_event(loop_id, project_dir, "loop_created", "step", None, asdict(state))
    _write_state(state, project_dir)
    result = propose(loop_id, project_dir, lock.lease_token)
    return _with_context(result, {"lease_token": lock.lease_token})


def propose(
    loop_id: str, project_dir: str, lease_token: str, recover_orphans: bool = False
) -> ProposeResult:
    """Reconcile first, then create exactly one pending action."""
    _ensure_valid_lease(loop_id, project_dir, lease_token)
    foreign = check_foreign_host(loop_id, project_dir)
    if foreign is not None:
        return _foreign_host_stop_result(loop_id, project_dir, foreign)
    reconcile_result = reconcile(
        loop_id, project_dir, lease_token, allow_side_effect_resolution=recover_orphans
    )
    state = load_state(loop_id, project_dir)
    if _apply_preproposal_safety_stop_if_needed(state, ""):
        _persist_preproposal_stop(loop_id, project_dir, state, clear_pending=True)
        action = Action.STOP.value
    else:
        if (
            recover_orphans
            and reconcile_result.action_taken == "rerun_required"
            and state.pending_action is not None
            and state.pending_action.action == Action.RUN_CHECKER.value
        ):
            return _pending_proposal_result(state, state.pending_action, project_dir)
        if state.pending_action is not None:
            raise ProtocolViolationError("pending action must be completed before propose")
        action = _next_action(state, project_dir)
        if _apply_preproposal_safety_stop_if_needed(state, action):
            _persist_preproposal_stop(loop_id, project_dir, state, clear_pending=False)
            action = Action.STOP.value
    action_id = f"act-{secrets.token_hex(ACTION_ID_BYTES)}"
    iteration = _next_action_iteration(state, action)
    params = _proposal_params(state, action, project_dir)
    new_state = copy.deepcopy(state)
    new_state.pending_action = PendingAction(action_id, action, state.phase, iteration, now_iso())
    new_state.state_version += 1
    new_state.updated_at = now_iso()
    append_journal_event(
        loop_id,
        project_dir,
        "pending",
        "step",
        action_id,
        {"action": action, "expected_phase": state.phase},
    )
    _write_state(new_state, project_dir)
    return ProposeResult(
        action=action,
        action_id=action_id,
        state_version=new_state.state_version,
        expected_phase=state.phase,
        phase=state.phase,
        iteration=iteration,
        context=_proposal_context(params),
    )


def complete(
    loop_id: str,
    project_dir: str,
    action_id: str,
    state_version: int,
    result: dict[str, Any],
    lease_token: str,
) -> CompleteResult:
    """Complete the pending action using journal-first ordering."""
    _ensure_valid_lease(loop_id, project_dir, lease_token)
    state = load_state(loop_id, project_dir)
    if state.last_completed_action and state.last_completed_action.action_id == action_id:
        action = _last_completed_action_name(loop_id, project_dir, state)
        return CompleteResult(
            True,
            True,
            state.last_completed_action.state_version_after,
            _complete_next_hint(action),
        )
    if _is_stale_complete(state, action_id, state_version):
        raise StaleActionError(f"stale action: {action_id}")

    assert state.pending_action is not None
    action = state.pending_action.action
    result = _normalize_complete_result(state, action, result, project_dir)
    if action == Action.RUN_CHECKER.value:
        validate_implementation_checker_result(state, result, project_dir)
    new_version = state.state_version + 1
    payload = _completed_payload(action, result)
    append_journal_event(loop_id, project_dir, "completed", _actor_for(action), action_id, payload)
    apply_action_effect(state, action, result, project_dir, loop_id, action_id)
    state.last_completed_action = LastCompletedAction(
        action_id=action_id,
        state_version_before=state_version,
        state_version_after=new_version,
        result_digest=_digest_json(result),
        completed_at=now_iso(),
    )
    state.pending_action = None
    state.state_version = new_version
    state.updated_at = now_iso()
    _write_state(state, project_dir)
    return CompleteResult(True, False, new_version, _complete_next_hint(action))


def reconcile(
    loop_id: str,
    project_dir: str,
    lease_token: str,
    allow_side_effect_resolution: bool = True,
) -> ReconcileOutcome:
    """Resolve orphaned pending actions without rerunning side-effectful maker actions."""
    _ensure_valid_lease(loop_id, project_dir, lease_token)
    state = load_state(loop_id, project_dir)
    if state.pending_action is None:
        return ReconcileOutcome("none", state.state_version)
    pending = state.pending_action
    completed = find_journal_event(loop_id, project_dir, pending.action_id, "completed")
    if completed is not None:
        return _reconcile_from_payload(loop_id, project_dir, state, completed, "journal")
    artifact = load_artifact(loop_id, project_dir, pending.action_id, "check_result.json")
    if artifact is not None and pending.action == Action.RUN_CHECKER.value:
        return _reconcile_from_artifact(loop_id, project_dir, state, pending.action_id, artifact)
    if pending.action == Action.RUN_CHECKER.value:
        return ReconcileOutcome("rerun_required", state.state_version)
    if not allow_side_effect_resolution:
        return ReconcileOutcome("unresolved_pending", state.state_version)
    return _mark_unresolved_pending(loop_id, project_dir, state)


def heartbeat(loop_id: str, project_dir: str, lease_token: str) -> bool:
    """Update heartbeat only; state_version is unchanged."""
    return heartbeat_lock(loop_id, project_dir, lease_token)


def resume(
    loop_id: str,
    project_dir: str,
    reset_counters: bool,
    owner_id: str,
    ttl_seconds: int,
    host: str | None = None,
) -> ResumeResult:
    """Resume a failed/stopped loop and issue a new lease."""
    if not reset_counters:
        raise InvalidStateError("resume requires reset_counters=True")
    state = load_state(loop_id, project_dir)
    if state.status not in {"failed", "stopped"}:
        raise InvalidStateError(f"cannot resume status={state.status}")
    _replace_lock(loop_id, project_dir, owner_id, ttl_seconds, host)
    lock = _read_lock(lock_path(loop_id, project_dir))
    if lock is None:
        raise LockNotFoundError("new lock not found")
    for name in list(state.guards):
        state.guards[name] = GuardCounters()
    state.status = "running"
    state.stop_reason = None
    state.pending_action = None
    state.state_version += 1
    state.updated_at = now_iso()
    append_journal_event(loop_id, project_dir, "resumed", "step", None, {"phase": state.phase})
    _write_state(state, project_dir)
    return ResumeResult(state, lock.lease_token)


def attach(loop_id: str, project_dir: str, owner_id: str, ttl_seconds: int) -> ProposeResult:
    """Reacquire a stale lease for running/waiting_external loops, then propose."""
    state = load_state(loop_id, project_dir)
    if state.status not in {"running", "waiting_external"}:
        raise InvalidStateError(f"cannot attach status={state.status}")
    lock = reacquire_lease(loop_id, project_dir, owner_id, ttl_seconds)
    result = propose(loop_id, project_dir, lock.lease_token, recover_orphans=True)
    return _with_context(result, {"lease_token": lock.lease_token})


def apply_action_effect(
    state: LoopState,
    action: str,
    result: dict[str, Any],
    project_dir: str | None = None,
    loop_id: str | None = None,
    action_id: str | None = None,
) -> None:
    """Apply a completed action to state."""
    if _apply_safety_stop_if_needed(state, action, result):
        return
    if action == Action.RUN_MAKER.value:
        state.status = "running"
        return
    if action == Action.RUN_CHECKER.value:
        _apply_checker_result(state, result, project_dir, loop_id, action_id)
        return
    if action == Action.WAIT_EXTERNAL_REVIEW.value:
        if _extract_check_result_payload(result) is not None:
            _apply_checker_result(state, result, project_dir, loop_id, action_id)
            return
        state.status = "waiting_external" if not result.get("completed") else "running"
        return
    if action == Action.ADVANCE_PHASE.value:
        _apply_advance_phase(state, result)
        return
    if action == Action.STOP.value:
        state.status = "stopped"
        state.stop_reason = str(result.get("stop_reason") or state.stop_reason or "safety_stop")


def evaluate_guards(
    state: LoopState,
    phase_check: PhaseCheckResult,
    phase_def: Any | None,
    config: dict[str, Any] | None,
) -> GuardDecision:
    """Evaluate infra failure, pass, no-progress, then iteration limit."""
    cfg = config or DEFAULT_CONFIG
    counters = state.guards.setdefault(state.phase, GuardCounters())
    max_infra = int(_nested(cfg, ("guards", "infrastructure_failure", "max_retries"), 3))
    if phase_check.infrastructure_failure:
        counters.infrastructure_failure_count += 1
        if counters.infrastructure_failure_count >= max_infra:
            return GuardDecision(
                _failure_disposition(phase_def), "infrastructure_failure_exhausted"
            )
        return GuardDecision("retry", "infrastructure_failure_retry")
    if phase_check.passed:
        counters.no_progress_streak = 0
        counters.last_signature = None
        counters.infrastructure_failure_count = 0
        return GuardDecision(_success_disposition(phase_def), next_phase=_success_next(phase_def))
    _update_phase_no_progress(counters, phase_check, phase_def)
    if counters.no_progress_streak >= _phase_no_progress_repeat(phase_def, cfg):
        return GuardDecision(_failure_disposition(phase_def), "no_progress")
    counters.iteration += 1
    if counters.iteration >= _phase_max_iterations(phase_def, cfg):
        return GuardDecision(_failure_disposition(phase_def), "max_iterations")
    return GuardDecision("continue", next_action=Action.RUN_MAKER.value)


def combine_check_results(
    results: list[CheckResult],
    pass_criteria: dict[str, int],
    required_layers: frozenset[str],
) -> PhaseCheckResult:
    """Aggregate checker layers, treating any missing required layer as infra failure."""
    layer_by_name = {result.layer: result for result in results}
    missing = [name for name in required_layers if name not in layer_by_name]
    if missing:
        return PhaseCheckResult(False, results, "", True)
    mechanical = layer_by_name.get("mechanical")
    llm_review = layer_by_name.get("llm_review")
    llm_ok = _llm_review_ok(llm_review, pass_criteria)
    infra = any(result.infrastructure_failure for result in results)
    signature = _combined_signature(mechanical, llm_review, llm_ok)
    mechanical_ok = True if mechanical is None else mechanical.passed
    return PhaseCheckResult(mechanical_ok and llm_ok and not infra, results, signature, infra)


def checker_pass_criteria(state: LoopState, project_dir: str) -> dict[str, int]:
    """Load the current phase LLM pass criteria through one durable path."""
    phase_def = _load_phase_definition(state, project_dir)
    lib_dir = Path(__file__).resolve().parent
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    import loop_definition

    return loop_definition.checker_pass_criteria(phase_def.checker)


def validate_implementation_checker_result(
    state: LoopState, result: dict[str, Any], project_dir: str | None = None
) -> None:
    """Validate the sealed issue-loop implementation checker contract."""
    if state.definition_id != "issue-loop" or state.phase != "implementation":
        return
    raw = _extract_check_result_payload(result)
    if not isinstance(raw, dict) or frozenset(raw) != _STRICT_PHASE_CHECK_KEYS:
        raise ProtocolViolationError("sealed checker result has invalid phase schema")
    if not isinstance(raw.get("passed"), bool):
        raise ProtocolViolationError("sealed checker result has invalid passed value")
    if not isinstance(raw.get("signature"), str) or not isinstance(
        raw.get("infrastructure_failure"), bool
    ):
        raise ProtocolViolationError("sealed checker result has invalid phase fields")
    reviewers = _strict_reviewer_manifest(raw.get("metadata"))
    results = raw.get("results")
    if not isinstance(results, list) or len(results) != len(_STRICT_CHECKER_LAYERS):
        raise ProtocolViolationError("sealed checker result must contain two layers")
    layers: set[str] = set()
    for item in results:
        layer = _validate_strict_check_result(item, reviewers)
        if layer in layers:
            raise ProtocolViolationError("sealed checker result contains duplicate layers")
        layers.add(layer)
    if layers != _STRICT_CHECKER_LAYERS:
        raise ProtocolViolationError("sealed checker result is missing required layers")
    durable_project_dir = project_dir or state.worktree_path
    _validate_checker_result_semantics(raw, checker_pass_criteria(state, durable_project_dir))


def _validate_checker_result_semantics(raw: dict[str, Any], pass_criteria: dict[str, int]) -> None:
    """Recompute checker semantics and reject contradictory serialized values."""
    phase_check = phase_check_from_dict(raw)
    layer_by_name = {item.layer: item for item in phase_check.results}
    mechanical = layer_by_name["mechanical"]
    llm_review = layer_by_name["llm_review"]
    if mechanical.findings:
        raise ProtocolViolationError("sealed checker mechanical findings must be empty")
    if any(item.infrastructure_failure and item.passed for item in phase_check.results):
        raise ProtocolViolationError("sealed checker infrastructure layer cannot pass")
    expected_llm_passed = _llm_review_ok(llm_review, pass_criteria) and not (
        llm_review.infrastructure_failure
    )
    if llm_review.passed != expected_llm_passed:
        raise ProtocolViolationError("sealed checker LLM passed value is inconsistent")
    expected_llm_signature = compute_llm_review_signature(llm_review.findings)
    if llm_review.signature != expected_llm_signature:
        raise ProtocolViolationError("sealed checker LLM signature is inconsistent")
    expected = combine_check_results(
        phase_check.results,
        pass_criteria,
        _STRICT_CHECKER_LAYERS,
    )
    if (
        phase_check.passed != expected.passed
        or phase_check.infrastructure_failure != expected.infrastructure_failure
        or phase_check.signature != expected.signature
    ):
        raise ProtocolViolationError("sealed checker aggregate result is inconsistent")


def _strict_reviewer_manifest(metadata: Any) -> frozenset[str]:
    """Return a validated reviewer manifest from phase metadata."""
    if not isinstance(metadata, dict) or frozenset(metadata) != {"reviewers"}:
        raise ProtocolViolationError("sealed checker reviewer manifest is required")
    reviewers = metadata.get("reviewers")
    if (
        not isinstance(reviewers, list)
        or not 1 <= len(reviewers) <= 2
        or not all(isinstance(item, str) and item for item in reviewers)
        or len(set(reviewers)) != len(reviewers)
        or "code-reviewer" not in reviewers
    ):
        raise ProtocolViolationError("sealed checker reviewer manifest is invalid")
    return frozenset(reviewers)


def _validate_strict_check_result(value: Any, reviewers: frozenset[str]) -> str:
    """Validate one serialized checker layer and return its name."""
    if not isinstance(value, dict) or frozenset(value) != CHECK_RESULT_KEYS:
        raise ProtocolViolationError("sealed checker layer schema is invalid")
    layer = value.get("layer")
    if layer not in _STRICT_CHECKER_LAYERS:
        raise ProtocolViolationError("sealed checker layer is invalid")
    if not isinstance(value.get("passed"), bool) or not isinstance(
        value.get("infrastructure_failure"), bool
    ):
        raise ProtocolViolationError("sealed checker layer status is invalid")
    if value.get("signature") is not None and not isinstance(value.get("signature"), str):
        raise ProtocolViolationError("sealed checker layer signature is invalid")
    if not isinstance(value.get("raw_artifact_path"), str):
        raise ProtocolViolationError("sealed checker artifact reference is invalid")
    findings = value.get("findings")
    if not isinstance(findings, list):
        raise ProtocolViolationError("sealed checker findings are invalid")
    for finding in findings:
        _validate_strict_finding(finding, reviewers if layer == "llm_review" else None)
    return str(layer)


def _validate_strict_finding(value: Any, reviewers: frozenset[str] | None) -> None:
    """Validate one serialized finding and optional reviewer binding."""
    if not isinstance(value, dict) or frozenset(value) != FINDING_KEYS:
        raise ProtocolViolationError("sealed checker finding schema is invalid")
    if value.get("severity") not in {"critical", "high", "medium", "low"}:
        raise ProtocolViolationError("sealed checker finding severity is invalid")
    if not isinstance(value.get("summary"), str) or not isinstance(value.get("source"), str):
        raise ProtocolViolationError("sealed checker finding text is invalid")
    if value.get("path") is not None and not isinstance(value.get("path"), str):
        raise ProtocolViolationError("sealed checker finding path is invalid")
    line = value.get("line")
    if line is not None and (not isinstance(line, int) or isinstance(line, bool)):
        raise ProtocolViolationError("sealed checker finding line is invalid")
    if reviewers is not None and value["source"] not in reviewers:
        raise ProtocolViolationError("sealed checker finding source is not a reviewer")


def extract_failed_test_ids(output: str) -> list[str]:
    """Extract pytest failed node ids."""
    return sorted(set(_FAILED_NODEID_RE.findall(output or "")))


def extract_lint_rule_ids(output: str) -> list[str]:
    """Extract ruff rule ids."""
    return sorted(set(_RUFF_RULE_RE.findall(output or "")))


def compute_implementation_signature(failures: list[MechanicalFailure]) -> str:
    """Compute the implementation no-progress signature."""
    per_command = sorted(_per_command_signature(failure) for failure in failures)
    return _short_hash("|".join(per_command))


def compute_llm_review_signature(findings: list[Finding]) -> str:
    """Compute a signature from critical/high LLM review findings only."""
    keys = sorted(_normalize_finding_key(f) for f in findings if f.severity in {"critical", "high"})
    return _short_hash("|".join(keys))


def compute_pr_review_signature(finding_signatures: list[str]) -> str:
    """Compute a PR review phase signature from finding signatures."""
    return _short_hash("|".join(sorted(set(finding_signatures))))


def normalize_pr_finding_signature(
    comment: dict[str, Any], dedup_config: dict[str, Any] | None = None
) -> str:
    """Normalize one PR review comment into a stable finding signature."""
    config = dedup_config or {}
    bucket_size = _positive_int(config.get("line_bucket_size"), 5)
    path = _normalize_pr_path(_optional_str(comment.get("path")))
    line_bucket = _pr_line_bucket(comment, bucket_size)
    body = _normalize_pr_body_for_hash(str(comment.get("body") or ""), config)
    return _short_hash(f"{path}:{line_bucket}:{body}")


def build_pr_iteration_findings(pr_review: dict[str, Any], iteration: int) -> IterationFindings:
    """Build current-iteration open signature summary from state.pr_review."""
    signatures: set[str] = set()
    new_count = 0
    for signature, record in _pr_findings_map(pr_review).items():
        if record.get("status") == "dismissed":
            continue
        if int(record.get("last_seen_iteration") or 0) == iteration:
            signatures.add(signature)
        if int(record.get("first_seen_iteration") or 0) == iteration:
            new_count += 1
    return IterationFindings(frozenset(signatures), new_count)


def evaluate_pr_review_no_progress(
    previous: IterationFindings, current: IterationFindings
) -> NoProgressResult:
    """Evaluate PR review no-progress by reraised signatures or new-count plateau."""
    reraised = current.signatures & previous.signatures
    if reraised:
        return NoProgressResult(True, "reraised", reraised)
    if current.new_count >= previous.new_count:
        return NoProgressResult(True, "new_count_non_decreasing")
    return NoProgressResult(False, "progress")


def run_mechanical_checks(
    commands: list[str],
    cwd: str,
    timeout_seconds: int,
    heartbeat: Callable[[], None] | None = None,
    artifact_writer: Callable[[int, str, str, int], None] | None = None,
) -> list[MechanicalFailure]:
    """Run mechanical checker commands and classify failures via failure_detector."""
    detector = _load_failure_detector()
    failures: list[MechanicalFailure] = []
    for index, command in enumerate(commands, start=1):
        try:
            output, exit_code = _run_mechanical_command(command, cwd, timeout_seconds)
            response = {"exit_code": exit_code, "stdout": output}
            result = detector.analyze("Bash", {"command": command}, response)
        finally:
            if heartbeat is not None:
                heartbeat()
        if artifact_writer is not None:
            artifact_writer(index, command, output, exit_code)
        if result is None:
            continue
        failures.append(
            MechanicalFailure(
                command=command,
                failure_type=str(result["failure_type"]),
                error_type=str(result["error_type"]),
                output=output,
            )
        )
    return failures


def save_artifact(loop_id: str, project_dir: str, action_id: str, name: str, content: str) -> str:
    """Write a redacted artifact with 0600 permissions and return its relative path."""
    path = artifact_path(loop_id, project_dir, action_id, name)
    _ensure_dir(path.parent)
    _write_text(path, redact(content))
    return str(Path("artifacts") / action_id / name)


def load_artifact(loop_id: str, project_dir: str, action_id: str, name: str) -> str | None:
    """Read an artifact, returning None if absent."""
    path = artifact_path(loop_id, project_dir, action_id, name)
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def append_journal_event(
    loop_id: str,
    project_dir: str,
    event: str,
    actor: str,
    action_id: str | None,
    payload: dict[str, Any],
) -> None:
    """Append one redacted journal event with owner-only permissions."""
    state = _read_json_file(state_path(loop_id, project_dir))
    record = {
        "ts": now_iso(),
        "loop_id": loop_id,
        "phase": str(state.get("phase") or ""),
        "iteration": int(state.get("iteration") or 0),
        "action_id": action_id,
        "event": event,
        "actor": actor,
        "payload": redact_payload(payload),
    }
    _append_jsonl(journal_path(loop_id, project_dir), record)


def find_journal_event(
    loop_id: str, project_dir: str, action_id: str, event: str
) -> dict[str, Any] | None:
    """Find the latest matching journal event."""
    path = journal_path(loop_id, project_dir)
    if not path.is_file():
        return None
    found: dict[str, Any] | None = None
    with path.open(encoding="utf-8") as f:
        for line in f:
            record = _loads_json_line(line)
            if record.get("action_id") == action_id and record.get("event") == event:
                found = record
    return found


def build_audit_payload(
    event_type: str,
    state: LoopState,
    action_id: str | None = None,
    maker: dict[str, Any] | None = None,
    checker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the loop_* audit payload shape; emitting is handled by later phases."""
    payload = {
        "event_type": event_type,
        "loop_id": state.loop_id,
        "definition_id": state.definition_id,
        "phase": state.phase,
        "iteration": state.iteration,
        "status": state.status,
        "action_id": action_id,
        "maker": maker or {},
        "checker": checker or {},
        "stop_reason": state.stop_reason,
    }
    return redact_payload(payload)


def emit_loop_audit_event(
    event_type: str,
    project_dir: str,
    payload: dict[str, Any],
    *,
    aid: str | None = None,
) -> dict[str, Any] | None:
    """Emit a loop audit event without letting audit failures break the loop protocol."""
    try:
        audit_hooks_dir = Path(__file__).resolve().parents[2] / "audit" / "hooks"
        if str(audit_hooks_dir) not in sys.path:
            sys.path.insert(0, str(audit_hooks_dir))
        import event_logger

        trace = event_logger.load_trace_state(project_dir)
        phase = payload.get("phase") if isinstance(payload.get("phase"), str) else None
        return event_logger.emit_event(
            event_type,
            redact_payload(payload),
            session_id=str(trace.get("session_id") or ""),
            tid=str(trace.get("tid") or ""),
            aid=aid,
            ctx={"skill": "loop-harness", "phase": phase},
            project_dir=project_dir,
        )
    except Exception:
        return None


def acquire_lock(
    loop_id: str,
    project_dir: str,
    owner_id: str,
    ttl_seconds: int,
    host: str | None = None,
) -> LockInfo | None:
    """Acquire a new lease using O_EXCL and TTL-only stale checks."""
    host = host or socket.gethostname()
    path = lock_path(loop_id, project_dir)
    _ensure_dir(path.parent)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    except FileExistsError:
        return _acquire_existing_lock(path, owner_id, ttl_seconds, host)
    return _write_new_lock_fd(fd, owner_id, ttl_seconds, host)


def reacquire_lease(
    loop_id: str,
    project_dir: str,
    owner_id: str,
    ttl_seconds: int,
    host: str | None = None,
) -> LockInfo:
    """Reacquire an existing stale lease for attach."""
    path = lock_path(loop_id, project_dir)
    if not path.exists():
        raise LockNotFoundError(str(path))
    existing = _read_lock(path)
    if existing is not None and is_lease_alive(existing):
        raise ForeignLeaseError(existing)
    _replace_lock(loop_id, project_dir, owner_id, ttl_seconds, host)
    lock = _read_lock(path)
    if lock is None:
        raise LockNotFoundError(str(path))
    return lock


def release_lock(loop_id: str, project_dir: str, lease_token: str) -> bool:
    """Release the lock only if the caller-held lease token matches."""
    path = lock_path(loop_id, project_dir)
    fd = _open_lock_for_update(path)
    if fd is None:
        return False
    try:
        with os.fdopen(fd, "r+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            if not _fd_matches_path(f.fileno(), path):
                return False
            lock = _read_lock_stream(f)
            if lock is None or lock.lease_token != lease_token:
                return False
            path.unlink()
            return True
    except OSError:
        return False


def heartbeat_lock(loop_id: str, project_dir: str, lease_token: str) -> bool:
    """Update heartbeat_at only when the caller-held lease token is valid."""
    path = lock_path(loop_id, project_dir)
    fd = _open_lock_for_update(path)
    if fd is None:
        return False
    try:
        with os.fdopen(fd, "r+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            if not _fd_matches_path(f.fileno(), path):
                return False
            lock = _read_lock_stream(f)
            if lock is None or lock.lease_token != lease_token or not is_lease_alive(lock):
                return False
            updated = LockInfo(
                lock.owner_id,
                lock.pid,
                lock.host,
                lock.started_at,
                now_iso(),
                lock.ttl,
                lock.lease_token,
            )
            _rewrite_locked_file(f, asdict(updated))
            return True
    except OSError:
        return False


def validate_lease(loop_id: str, project_dir: str, lease_token: str) -> bool:
    """Validate the explicit caller-held lease token against lock.json."""
    lock = _read_lock(lock_path(loop_id, project_dir))
    if lock is None or not lease_token:
        return False
    return lock.lease_token == lease_token and is_lease_alive(lock)


def is_lease_alive(lock: LockInfo, now: float | None = None) -> bool:
    """Return True when heartbeat_at + ttl is in the future; PID is ignored."""
    heartbeat = _parse_epoch(lock.heartbeat_at)
    if heartbeat is None:
        return False
    return heartbeat + lock.ttl > (time.time() if now is None else now)


def check_foreign_host(
    loop_id: str, project_dir: str, local_host: str | None = None
) -> LockInfo | None:
    """Return a live lock owned by another host, if present."""
    lock = _read_lock(lock_path(loop_id, project_dir))
    host = local_host or socket.gethostname()
    if lock is None or lock.host == host or not is_lease_alive(lock):
        return None
    return lock


def _initial_state(
    loop_id: str,
    definition_id: str,
    repo_identity_hash: str,
    worktree_path: str,
    branch: str,
    phase: str,
) -> LoopState:
    """Create an initial pending state."""
    now = now_iso()
    return LoopState(
        schema_version=SCHEMA_VERSION,
        loop_id=loop_id,
        definition_id=definition_id,
        repo_identity_hash=repo_identity_hash,
        phase=phase,
        iteration=0,
        status="pending",
        worktree_path=worktree_path,
        branch=branch,
        pr_number=None,
        guards={phase: GuardCounters()},
        last_check_result=None,
        pending_action=None,
        last_completed_action=None,
        stop_reason=None,
        pr_review=None,
        ignored_untrusted_comment_count=0,
        created_at=now,
        updated_at=now,
        state_version=0,
    )


def _with_context(result: ProposeResult, values: dict[str, Any]) -> ProposeResult:
    """Return a ProposeResult with added context."""
    context = {**result.context, **values}
    return ProposeResult(
        action=result.action,
        action_id=result.action_id,
        state_version=result.state_version,
        expected_phase=result.expected_phase,
        phase=result.phase,
        iteration=result.iteration,
        context=context,
    )


def _ensure_valid_lease(loop_id: str, project_dir: str, lease_token: str) -> None:
    """Raise when lease validation fails."""
    if not validate_lease(loop_id, project_dir, lease_token):
        raise WriteRejectedError(f"invalid lease for {loop_id}")


def _is_stale_complete(state: LoopState, action_id: str, state_version: int) -> bool:
    """Return True if complete arguments do not match pending state."""
    pending = state.pending_action
    return pending is None or pending.action_id != action_id or state.state_version != state_version


def _completed_payload(action: str, result: dict[str, Any]) -> dict[str, Any]:
    """Build completed journal payload."""
    payload = {"action": action, "result": result}
    check_result = _extract_check_result_payload(result)
    if check_result is not None:
        payload["check_result"] = check_result
    return payload


def _normalize_complete_result(
    state: LoopState, action: str, result: dict[str, Any], project_dir: str
) -> dict[str, Any]:
    """Validate and normalize action result before writing durable journal records."""
    if action != Action.ADVANCE_PHASE.value or _has_failed_push_guard(result):
        return result
    if result.get("pr_number") is None:
        if _advance_requires_pr_number(state, result, project_dir):
            raise ValueError("pr_number is required for external review phase")
        return result
    normalized = dict(result)
    normalized["pr_number"] = _coerce_pr_number(result["pr_number"])
    return normalized


def _has_failed_push_guard(result: dict[str, Any]) -> bool:
    """Return True when an advance completion reports a failed push guard."""
    guard = result.get("push_guard")
    return isinstance(guard, dict) and not (
        guard.get("branch_ok", True) and guard.get("repo_identity_ok", True)
    )


def _advance_requires_pr_number(state: LoopState, result: dict[str, Any], project_dir: str) -> bool:
    """Return True when the next phase requires a PR number for external review."""
    next_phase = _pending_next_phase(state) or result.get("next_phase")
    if not next_phase:
        return False
    phase_def = _load_phase_definition_by_name(state, project_dir, str(next_phase))
    return isinstance(_phase_nested(phase_def, ("checker", "external_signal"), None), dict)


def _complete_next_hint(action: str) -> str:
    """Return the next hint after completing an action."""
    if action in {
        Action.EXIT_SUCCESS.value,
        Action.EXIT_FAILURE.value,
        Action.STOP.value,
    }:
        return "loop terminal"
    return "call propose again"


def _actor_for(action: str) -> str:
    """Return journal actor for an action."""
    if action == Action.RUN_MAKER.value:
        return "maker"
    if action == Action.RUN_CHECKER.value:
        return "checker"
    if action == Action.WAIT_EXTERNAL_REVIEW.value:
        return "waiter"
    return "step"


def _next_action(state: LoopState, project_dir: str) -> str:
    """Select the next action from state."""
    if state.status == "pending":
        return Action.RUN_MAKER.value
    if state.status == "waiting_external":
        return Action.WAIT_EXTERNAL_REVIEW.value
    if state.status == "passed":
        return Action.EXIT_SUCCESS.value
    if state.status == "failed":
        return Action.EXIT_FAILURE.value
    if state.status == "stopped":
        return Action.STOP.value
    if state.status == "running" and _pending_next_phase(state):
        return Action.ADVANCE_PHASE.value
    last_action = _last_completed_action_name(state.loop_id, project_dir, state)
    if (
        _phase_uses_external_signal(state, project_dir)
        and last_action != Action.WAIT_EXTERNAL_REVIEW.value
    ):
        return Action.WAIT_EXTERNAL_REVIEW.value
    if last_action == Action.RUN_MAKER.value:
        return Action.RUN_CHECKER.value
    return Action.RUN_MAKER.value


def _pending_proposal_result(
    state: LoopState, pending: PendingAction, project_dir: str
) -> ProposeResult:
    """Return an existing rerunnable pending action without creating a new action."""
    params = _proposal_params(state, pending.action, project_dir)
    return ProposeResult(
        action=pending.action,
        action_id=pending.action_id,
        state_version=state.state_version,
        expected_phase=pending.phase,
        phase=pending.phase,
        iteration=pending.iteration,
        context=_proposal_context(params),
    )


def _apply_preproposal_safety_stop_if_needed(state: LoopState, action: str) -> bool:
    """Stop before proposing unsafe advance params."""
    if _repo_identity_hash(state.worktree_path) != state.repo_identity_hash:
        state.status = "stopped"
        state.stop_reason = "repo_identity_mismatch"
        return True
    if action not in {Action.ADVANCE_PHASE.value, Action.WAIT_EXTERNAL_REVIEW.value}:
        return False
    if _current_branch(state.worktree_path) != state.branch:
        state.status = "stopped"
        state.stop_reason = "push_guard_violation"
        return True
    return False


def _persist_preproposal_stop(
    loop_id: str,
    project_dir: str,
    state: LoopState,
    *,
    clear_pending: bool,
) -> None:
    """Persist a safety stop before issuing its terminal proposal."""
    replaced = state.pending_action if clear_pending else None
    if clear_pending:
        state.pending_action = None
    payload = {"stop_reason": state.stop_reason}
    if replaced is not None:
        payload.update(
            {
                "replaced_action_id": replaced.action_id,
                "replaced_action": replaced.action,
            }
        )
    append_journal_event(
        loop_id,
        project_dir,
        "stopped",
        "step",
        None,
        payload,
    )
    state.state_version += 1
    state.updated_at = now_iso()
    _write_state(state, project_dir)


def _last_completed_action_name(loop_id: str, project_dir: str, state: LoopState) -> str:
    """Infer last completed action from last_check_result and status."""
    if state.last_completed_action is not None:
        event = find_journal_event(
            loop_id, project_dir, state.last_completed_action.action_id, "completed"
        )
        payload = event.get("payload") if isinstance(event, dict) else {}
        if isinstance(payload, dict) and payload.get("action"):
            return str(payload["action"])
    return ""


def _next_action_iteration(state: LoopState, action: str) -> int:
    """Return action iteration number."""
    counters = state.guards.setdefault(state.phase, GuardCounters())
    if action == Action.RUN_MAKER.value:
        return counters.iteration + 1
    return max(counters.iteration, 1)


def _proposal_params(state: LoopState, action: str, project_dir: str) -> dict[str, Any]:
    """Build action-specific proposal params from durable state and loop definition."""
    if action == Action.STOP.value:
        return {"stop_reason": _normalize_stop_reason(state.stop_reason or "safety_stop")}
    if action == Action.EXIT_SUCCESS.value:
        return {"pr_number": state.pr_number}

    phase_def = _load_phase_definition(state, project_dir)
    if action == Action.RUN_MAKER.value:
        return {
            "maker_agent": _phase_nested(phase_def, ("maker", "agent"), None),
            "prompt_template": _phase_nested(phase_def, ("maker", "prompt_template"), None),
            "worktree_path": state.worktree_path,
            "branch": state.branch,
        }
    if action == Action.RUN_CHECKER.value:
        checker = _phase_nested(phase_def, ("checker",), {})
        return copy.deepcopy(checker) if isinstance(checker, dict) else {}
    if action == Action.WAIT_EXTERNAL_REVIEW.value:
        external = _phase_nested(phase_def, ("checker", "external_signal"), {})
        params = copy.deepcopy(external) if isinstance(external, dict) else {}
        config = _load_loop_config(project_dir)
        params.setdefault(
            "poll_interval_seconds",
            _nested(config, ("pr_review", "poll_interval_seconds"), 120),
        )
        params.setdefault(
            "timeout_seconds",
            _nested(config, ("pr_review", "timeout_seconds"), 3600),
        )
        params["pr_number"] = state.pr_number
        params["push_required"] = (
            _last_completed_action_name(state.loop_id, project_dir, state) == Action.RUN_MAKER.value
        )
        params["verified_branch"] = state.branch
        return params
    if action == Action.ADVANCE_PHASE.value:
        return {
            "verified_branch": state.branch,
            "next_phase": _pending_next_phase(state)
            or _phase_nested(phase_def, ("on_success", "next"), None),
            "exec": copy.deepcopy(_phase_nested(phase_def, ("on_success", "exec"), [])),
        }
    if action == Action.EXIT_FAILURE.value:
        return {
            "pr_number": state.pr_number,
            "stop_reason": state.stop_reason or "guard_failed",
            "draft_pr_exec": copy.deepcopy(_phase_nested(phase_def, ("on_failure", "exec"), [])),
        }
    return {}


def _proposal_context(params: dict[str, Any]) -> dict[str, Any]:
    """Expose params both nested and legacy top-level, without reserved-key collisions."""
    return {
        "params": params,
        **{k: v for k, v in params.items() if k not in _RESERVED_PROPOSAL_CONTEXT_KEYS},
    }


def _load_phase_definition(state: LoopState, project_dir: str) -> Any:
    """Load the current phase definition, failing closed on config drift."""
    return _load_phase_definition_by_name(state, project_dir, state.phase)


def _load_phase_definition_by_name(state: LoopState, project_dir: str, phase: str) -> Any:
    """Load a named phase definition, failing closed on config drift."""
    lib_dir = Path(__file__).resolve().parent
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    import loop_definition

    definition = loop_definition.load_all_definitions(project_dir).get(state.definition_id)
    if definition is None:
        raise InvalidStateError(f"loop definition not found: {state.definition_id}")
    return loop_definition.phase_by_name(definition, phase)


def _phase_uses_external_signal(state: LoopState, project_dir: str) -> bool:
    """Return True when the current phase checker is an external wait."""
    phase_def = _load_phase_definition(state, project_dir)
    return isinstance(_phase_nested(phase_def, ("checker", "external_signal"), None), dict)


def _load_loop_config(project_dir: str) -> dict[str, Any]:
    """Load loop-harness config, including package defaults and project override."""
    lib_dir = Path(__file__).resolve().parent
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    import loop_definition

    return loop_definition.load_config(project_dir)


def _current_branch(worktree_path: str) -> str:
    """Return the current branch for a loop worktree."""
    return _git_stdout(["branch", "--show-current"], worktree_path)


def _repo_identity_hash(project_dir: str) -> str:
    """Return the repository identity hash using the worktree manager algorithm."""
    material = _repo_identity_material(project_dir)
    if not material:
        material = str(Path(project_dir).resolve())
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


def is_repo_identity_verified(state: LoopState) -> bool:
    """Verify state repo identity without accepting a path-only fallback."""
    material = _repo_identity_material(state.worktree_path)
    if not material:
        return False
    actual = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    return actual == state.repo_identity_hash


def _repo_identity_material(project_dir: str) -> str:
    """Return stable git identity material, or empty when git cannot verify it."""
    material = _git_stdout(["config", "--get", "remote.origin.url"], project_dir)
    if material:
        return material
    return _git_stdout(["rev-parse", "--path-format=absolute", "--git-common-dir"], project_dir)


def _git_stdout(args: list[str], cwd: str) -> str:
    """Run git and return stdout, or an empty string on failure."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _normalize_stop_reason(value: str) -> str:
    """Normalize safety stop reasons to the public CLI enum."""
    if value in _PUBLIC_STOP_REASONS:
        return value
    if value in {"default_branch", "push_guard_failed"}:
        return "push_guard_violation"
    return "safety_stop"


def _push_guard_stop_reason(guard: dict[str, Any]) -> str:
    """Return the public safety stop reason for push guard failure."""
    if not guard.get("repo_identity_ok", True):
        return "repo_identity_mismatch"
    return "push_guard_violation"


def _apply_safety_stop_if_needed(state: LoopState, action: str, result: dict[str, Any]) -> bool:
    """Apply safety stop for push guard violations."""
    guard = result.get("push_guard")
    if action not in {
        Action.ADVANCE_PHASE.value,
        Action.WAIT_EXTERNAL_REVIEW.value,
    } or not isinstance(guard, dict):
        return False
    if guard.get("branch_ok", True) and guard.get("repo_identity_ok", True):
        return False
    state.status = "stopped"
    state.stop_reason = _push_guard_stop_reason(guard)
    return True


def _pending_next_phase(state: LoopState) -> str | None:
    """Return the durable pending phase advance target, if any."""
    if not isinstance(state.last_check_result, dict):
        return None
    value = state.last_check_result.get("next_phase")
    return str(value) if value else None


def _coerce_pr_number(value: Any) -> int:
    """Return a positive PR number or reject invalid advance results before journaling."""
    if isinstance(value, bool):
        raise ValueError("pr_number must be a positive integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise ValueError("pr_number must be a positive integer")
    if number < 1:
        raise ValueError("pr_number must be a positive integer")
    return number


def _apply_advance_phase(state: LoopState, result: dict[str, Any]) -> None:
    """Apply a previously proposed phase advance."""
    if result.get("pr_number") is not None:
        state.pr_number = _coerce_pr_number(result["pr_number"])
    next_phase = _pending_next_phase(state) or result.get("next_phase")
    if not next_phase:
        state.status = "running"
        return
    state.phase = str(next_phase)
    state.guards.setdefault(state.phase, GuardCounters())
    state.status = "running"
    if isinstance(state.last_check_result, dict):
        state.last_check_result.pop("next_phase", None)


def _apply_checker_result(
    state: LoopState,
    result: dict[str, Any],
    project_dir: str | None,
    loop_id: str | None,
    action_id: str | None,
) -> None:
    """Apply checker result and guard decision."""
    phase_check = _phase_check_from_result(result)
    state.last_check_result = phase_check_to_dict(phase_check)
    phase_def = result.get("phase_def")
    config = result.get("config")
    if project_dir:
        phase_def = _load_phase_definition(state, project_dir)
        config = _load_loop_config(project_dir)
    decision = evaluate_guards(state, phase_check, phase_def, config)
    if decision.disposition == "continue" or decision.disposition == "retry":
        state.status = "running"
        return
    if decision.disposition == Action.ADVANCE_PHASE.value:
        if decision.next_phase:
            state.last_check_result["next_phase"] = decision.next_phase
        state.status = "running"
        return
    if decision.disposition == Action.EXIT_SUCCESS.value:
        _require_journal_consistency(project_dir, loop_id, action_id, state.last_check_result)
        state.status = "passed"
        return
    state.status = "failed"
    state.stop_reason = decision.reason or "guard_failed"


def _phase_check_from_result(result: dict[str, Any]) -> PhaseCheckResult:
    """Coerce a dict result to PhaseCheckResult."""
    raw = _extract_check_result_dict(result) or result
    return phase_check_from_dict(raw)


def _extract_check_result_dict(result: dict[str, Any]) -> dict[str, Any] | None:
    """Return check_result from a result wrapper if present."""
    check_result = result.get("check_result")
    return check_result if isinstance(check_result, dict) else None


def _extract_check_result_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    """Return wrapper or raw PhaseCheckResult payload from a completion result."""
    check_result = _extract_check_result_dict(result)
    if check_result is not None:
        return check_result
    return result if _is_phase_check_result_dict(result) else None


def _is_phase_check_result_dict(result: dict[str, Any]) -> bool:
    """Return True for the raw PhaseCheckResult shape."""
    return (
        isinstance(result.get("passed"), bool)
        and isinstance(result.get("signature"), str)
        and isinstance(result.get("infrastructure_failure"), bool)
        and isinstance(result.get("results"), list)
    )


def _require_journal_consistency(
    project_dir: str | None,
    loop_id: str | None,
    action_id: str | None,
    check_result: dict[str, Any] | None,
) -> None:
    """Check state/journal digest only for passed transitions."""
    if not project_dir or not loop_id or not action_id or check_result is None:
        raise IntegrityError("missing journal consistency inputs")
    digest = _digest_json(redact_payload(check_result))
    if not _verify_journal_consistency(loop_id, project_dir, action_id, digest):
        raise IntegrityError("journal check_result digest mismatch")


def _verify_journal_consistency(
    loop_id: str, project_dir: str, action_id: str, check_result_digest: str
) -> bool:
    """Verify completed journal check_result digest before passed transition."""
    event = find_journal_event(loop_id, project_dir, action_id, "completed")
    if event is None:
        return False
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    stored = payload.get("check_result") if isinstance(payload, dict) else None
    return _digest_json(stored) == check_result_digest


def _reconcile_from_payload(
    loop_id: str,
    project_dir: str,
    state: LoopState,
    event: dict[str, Any],
    source: str,
) -> ReconcileOutcome:
    """Resolve pending action from completed journal payload."""
    pending = state.pending_action
    if pending is None:
        return ReconcileOutcome("none", state.state_version)
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    apply_action_effect(state, pending.action, result, project_dir, loop_id, pending.action_id)
    return _finalize_reconciled(loop_id, project_dir, state, source, pending.action_id, result)


def _reconcile_from_artifact(
    loop_id: str, project_dir: str, state: LoopState, action_id: str, artifact: str
) -> ReconcileOutcome:
    """Resolve pending checker action from check_result.json artifact."""
    try:
        result = json.loads(artifact)
    except json.JSONDecodeError as exc:
        raise IntegrityError("invalid check_result artifact") from exc
    if not isinstance(result, dict):
        raise IntegrityError("invalid check_result artifact")
    try:
        validate_implementation_checker_result(state, result, project_dir)
    except ProtocolViolationError as exc:
        raise IntegrityError(f"invalid sealed checker artifact: {exc}") from exc
    apply_action_effect(state, Action.RUN_CHECKER.value, result, project_dir, loop_id, action_id)
    return _finalize_reconciled(loop_id, project_dir, state, "artifact", action_id, result)


def _finalize_reconciled(
    loop_id: str,
    project_dir: str,
    state: LoopState,
    source: str,
    action_id: str,
    result: dict[str, Any],
) -> ReconcileOutcome:
    """Append reconciled journal event and write state."""
    previous_version = state.state_version
    state.pending_action = None
    state.state_version += 1
    state.updated_at = now_iso()
    state.last_completed_action = LastCompletedAction(
        action_id=action_id,
        state_version_before=previous_version,
        state_version_after=state.state_version,
        result_digest=_digest_json(result),
        completed_at=now_iso(),
    )
    append_journal_event(
        loop_id, project_dir, "reconciled", "step", action_id, {"resolved_by": source}
    )
    _write_state(state, project_dir)
    return ReconcileOutcome(f"resolved_from_{source}", state.state_version)


def _mark_unresolved_pending(loop_id: str, project_dir: str, state: LoopState) -> ReconcileOutcome:
    """Mark side-effectful unresolved pending action as infrastructure failure."""
    phase_check = PhaseCheckResult(False, [], "pending_action_unresolved_after_crash", True)
    state.last_check_result = phase_check_to_dict(phase_check)
    decision = evaluate_guards(state, phase_check, None, DEFAULT_CONFIG)
    if decision.disposition == Action.EXIT_FAILURE.value:
        state.status = "failed"
        state.stop_reason = decision.reason
    state.pending_action = None
    state.state_version += 1
    state.updated_at = now_iso()
    append_journal_event(
        loop_id,
        project_dir,
        "reconciled",
        "step",
        None,
        {"resolved_by": "infrastructure_failure", "reason": phase_check.signature},
    )
    _write_state(state, project_dir)
    return ReconcileOutcome("marked_infrastructure_failure", state.state_version)


def _update_phase_no_progress(
    counters: GuardCounters, phase_check: PhaseCheckResult, phase_def: Any | None
) -> None:
    """Update no-progress counters using phase-specific signature semantics."""
    if _phase_no_progress_signature_kind(phase_def) == "pr_review" or _has_pr_review_metadata(
        phase_check
    ):
        _update_pr_review_no_progress(counters, phase_check)
        return
    _update_no_progress(counters, phase_check.signature)


def _update_no_progress(counters: GuardCounters, signature: str) -> None:
    """Update generic exact-signature no-progress counters."""
    if signature == counters.last_signature:
        counters.no_progress_streak += 1
        return
    counters.no_progress_streak = 1
    counters.last_signature = signature


def _update_pr_review_no_progress(counters: GuardCounters, phase_check: PhaseCheckResult) -> None:
    """Update no-progress counters from PR review metadata."""
    current = _metadata_iteration_findings(phase_check.metadata.get("current_iteration_findings"))
    previous = _metadata_iteration_findings(phase_check.metadata.get("previous_iteration_findings"))
    if current is None or previous is None:
        _update_no_progress(counters, phase_check.signature)
        return
    result = evaluate_pr_review_no_progress(previous, current)
    if result.no_progress:
        counters.no_progress_streak = (
            counters.no_progress_streak + 1 if counters.no_progress_streak else 1
        )
        counters.last_signature = phase_check.signature
        return
    counters.no_progress_streak = 0
    counters.last_signature = phase_check.signature


def _has_pr_review_metadata(phase_check: PhaseCheckResult) -> bool:
    """Return True when PhaseCheckResult carries PR review iteration metadata."""
    return (
        "current_iteration_findings" in phase_check.metadata
        and "previous_iteration_findings" in phase_check.metadata
    )


def _phase_max_iterations(phase_def: Any | None, config: dict[str, Any]) -> int:
    """Return phase max iterations."""
    return int(
        _phase_nested(
            phase_def,
            ("guards", "max_iterations"),
            _nested(config, ("guards", "max_iterations"), 3),
        )
    )


def _phase_no_progress_repeat(phase_def: Any | None, config: dict[str, Any]) -> int:
    """Return no-progress repeat threshold."""
    return int(
        _phase_nested(
            phase_def,
            ("guards", "no_progress", "repeat"),
            _nested(config, ("guards", "no_progress", "repeat"), 2),
        )
    )


def _phase_no_progress_signature_kind(phase_def: Any | None) -> str:
    """Return the no-progress signature kind for a phase."""
    return str(_phase_nested(phase_def, ("guards", "no_progress", "signature"), "implementation"))


def _success_disposition(phase_def: Any | None) -> str:
    """Return on_success disposition."""
    return str(_phase_nested(phase_def, ("on_success", "disposition"), Action.EXIT_SUCCESS.value))


def _success_next(phase_def: Any | None) -> str | None:
    """Return on_success next phase."""
    value = _phase_nested(phase_def, ("on_success", "next"), None)
    return str(value) if value else None


def _failure_disposition(phase_def: Any | None) -> str:
    """Return on_failure disposition."""
    return str(_phase_nested(phase_def, ("on_failure", "disposition"), Action.EXIT_FAILURE.value))


def _phase_nested(source: Any | None, path: tuple[str, ...], default: Any) -> Any:
    """Read nested values from dicts or dataclasses."""
    current = source
    for key in path:
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(key)
            continue
        current = getattr(current, key, None)
    return default if current is None else current


def _nested(source: dict[str, Any], path: tuple[str, ...], default: Any) -> Any:
    """Read a nested mapping value."""
    current: Any = source
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _llm_review_ok(result: CheckResult | None, pass_criteria: dict[str, int]) -> bool:
    """Return True if LLM findings meet critical/high criteria."""
    if result is None:
        return True
    crit = sum(1 for f in result.findings if f.severity == "critical")
    high = sum(1 for f in result.findings if f.severity == "high")
    return crit <= pass_criteria.get("critical", 0) and high <= pass_criteria.get("high", 0)


def _combined_signature(
    mechanical: CheckResult | None, llm_review: CheckResult | None, llm_ok: bool
) -> str:
    """Return phase signature from the failing layer."""
    if mechanical is not None and not mechanical.passed:
        return mechanical.signature or ""
    if llm_review is not None and not llm_ok:
        return compute_llm_review_signature(llm_review.findings)
    return ""


def _normalize_excerpt_for_hash(excerpt: str) -> str:
    """Remove volatile values before fallback hashing."""
    text = re.sub(r"/(tmp|private/var/folders)/\S+", "<TMP>", excerpt or "")
    text = re.sub(r"0x[0-9a-fA-F]+", "<ADDR>", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?\b", "<TS>", text)
    text = re.sub(r":\d+:", ":<LINE>:", text)
    return text.strip()


def _per_command_signature(failure: MechanicalFailure) -> str:
    """Compute one command signature."""
    if failure.failure_type == "test_failure":
        ids = extract_failed_test_ids(failure.output)
        material = f"{failure.failure_type}|{failure.error_type}|{','.join(ids)}" if ids else ""
    elif failure.failure_type == "lint_failure":
        ids = extract_lint_rule_ids(failure.output)
        material = f"{failure.failure_type}|{','.join(ids)}" if ids else ""
    else:
        material = ""
    if not material:
        material = (
            f"{failure.failure_type}|{failure.error_type}|"
            f"excerpt:{_normalize_excerpt_for_hash(failure.output)[:2000]}"
        )
    return _short_hash(material)


def _normalize_finding_key(finding: Finding) -> str:
    """Normalize one finding into a stable key."""
    path = finding.path.lstrip("./").replace("\\", "/") if finding.path else "__general__"
    line = (finding.line // 5) * 5 if finding.line is not None else "__none__"
    body = _normalize_excerpt_for_hash(finding.summary)
    return f"{path}:{line}:{body}"


def _normalize_pr_path(path: str | None) -> str:
    """Normalize a PR review file path."""
    if not path:
        return "__general__"
    return path.replace("\\", "/").removeprefix("./")


def _pr_line_bucket(comment: dict[str, Any], bucket_size: int) -> str:
    """Return the PR review line bucket, falling back to original_line."""
    line = _int_or_none(comment.get("line"))
    if line is None:
        line = _int_or_none(comment.get("original_line"))
    if line is None:
        return "__none__"
    return str((line // bucket_size) * bucket_size)


def _normalize_pr_body_for_hash(body: str, config: dict[str, Any]) -> str:
    """Normalize PR review body text for stable signature hashing."""
    text = re.sub(r"```.*?```", " ", body or "", flags=re.DOTALL)
    text = re.sub(r"https?://\S+", " ", text)
    for pattern in _string_list(config.get("signature_footer_patterns")) or [r"(?ms)^---\s*$.*\Z"]:
        text = re.sub(pattern, " ", text)
    text = re.sub(r"[#>*_`~\[\](){}:;,.!?/\\|+=-]", " ", text)
    tokens = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    stopwords = _default_pr_stopwords() | set(_string_list(config.get("stopwords_en")))
    stopwords |= set(_string_list(config.get("stopwords_ja")))
    return " ".join(sorted(set(token for token in tokens if token not in stopwords)))


def _default_pr_stopwords() -> set[str]:
    """Return built-in high-frequency stopwords for PR finding signatures."""
    return {
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


def _pr_findings_map(pr_review: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return a sanitized PR review findings map."""
    values = pr_review.get("findings")
    if not isinstance(values, dict):
        return {}
    return {
        str(key): dict(value)
        for key, value in values.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _iteration_findings_to_dict(value: IterationFindings) -> dict[str, Any]:
    """Serialize IterationFindings for PhaseCheckResult metadata."""
    return {"signatures": sorted(value.signatures), "new_count": value.new_count}


def _metadata_iteration_findings(value: Any) -> IterationFindings | None:
    """Deserialize IterationFindings from PhaseCheckResult metadata."""
    if isinstance(value, IterationFindings):
        return value
    if not isinstance(value, dict):
        return None
    signatures = value.get("signatures")
    if not isinstance(signatures, list):
        return None
    return IterationFindings(
        frozenset(str(signature) for signature in signatures),
        int(value.get("new_count") or 0),
    )


def _int_or_none(value: Any) -> int | None:
    """Return an integer unless the value is invalid or bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _optional_str(value: Any) -> str | None:
    """Return a non-empty string or None."""
    return value if isinstance(value, str) and value else None


def _string_list(value: Any) -> list[str]:
    """Normalize a scalar/list config value to a string list."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


def _positive_int(value: Any, default: int) -> int:
    """Return a positive integer or a default."""
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _short_hash(material: str) -> str:
    """Return a short sha256 digest."""
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def _digest_json(value: Any) -> str:
    """Return sha256 digest for canonical JSON."""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def phase_check_to_dict(result: PhaseCheckResult) -> dict[str, Any]:
    """Serialize PhaseCheckResult."""
    data = {
        "passed": result.passed,
        "signature": result.signature,
        "infrastructure_failure": result.infrastructure_failure,
        "results": [check_result_to_dict(item) for item in result.results],
    }
    if result.metadata:
        data["metadata"] = result.metadata
    return data


def phase_check_from_dict(data: dict[str, Any]) -> PhaseCheckResult:
    """Deserialize PhaseCheckResult from JSON-like dict."""
    return PhaseCheckResult(
        passed=bool(data.get("passed")),
        results=[check_result_from_dict(item) for item in data.get("results") or []],
        signature=str(data.get("signature") or ""),
        infrastructure_failure=bool(data.get("infrastructure_failure")),
        metadata=dict(data.get("metadata") or {}),
    )


def check_result_to_dict(result: CheckResult) -> dict[str, Any]:
    """Serialize CheckResult."""
    return {
        "passed": result.passed,
        "layer": result.layer,
        "signature": result.signature,
        "findings": [asdict(finding) for finding in result.findings],
        "raw_artifact_path": result.raw_artifact_path,
        "infrastructure_failure": result.infrastructure_failure,
    }


def check_result_from_dict(data: dict[str, Any]) -> CheckResult:
    """Deserialize CheckResult."""
    findings = [Finding(**item) for item in data.get("findings") or []]
    return CheckResult(
        passed=bool(data.get("passed")),
        layer=data.get("layer", "mechanical"),
        signature=data.get("signature"),
        findings=findings,
        raw_artifact_path=str(data.get("raw_artifact_path") or ""),
        infrastructure_failure=bool(data.get("infrastructure_failure")),
    )


def _state_to_dict(state: LoopState) -> dict[str, Any]:
    """Serialize LoopState."""
    counters = state.guards.get(state.phase)
    current_iteration = counters.iteration if counters is not None else 0
    if state.pending_action is not None:
        current_iteration = state.pending_action.iteration
    data = asdict(state)
    data["iteration"] = current_iteration
    data["guards"] = {name: asdict(counter) for name, counter in state.guards.items()}
    return data


def _state_from_dict(data: dict[str, Any]) -> LoopState:
    """Deserialize LoopState."""
    guards = {name: GuardCounters(**value) for name, value in (data.get("guards") or {}).items()}
    pending = data.get("pending_action")
    completed = data.get("last_completed_action")
    return LoopState(
        schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
        loop_id=str(data["loop_id"]),
        definition_id=str(data["definition_id"]),
        repo_identity_hash=str(data["repo_identity_hash"]),
        phase=str(data["phase"]),
        iteration=int(data.get("iteration") or 0),
        status=str(data["status"]),
        worktree_path=str(data["worktree_path"]),
        branch=str(data["branch"]),
        pr_number=data.get("pr_number"),
        guards=guards,
        last_check_result=data.get("last_check_result"),
        pending_action=PendingAction(**pending) if isinstance(pending, dict) else None,
        last_completed_action=LastCompletedAction(**completed)
        if isinstance(completed, dict)
        else None,
        stop_reason=data.get("stop_reason"),
        pr_review=data.get("pr_review"),
        ignored_untrusted_comment_count=int(data.get("ignored_untrusted_comment_count") or 0),
        created_at=str(data["created_at"]),
        updated_at=str(data["updated_at"]),
        state_version=int(data.get("state_version") or 0),
    )


def _write_state(state: LoopState, project_dir: str) -> None:
    """Write state.json atomically with 0600 permissions."""
    _write_json_file(state_path(state.loop_id, project_dir), _state_to_dict(state))


def _read_json_file(path: Path) -> dict[str, Any]:
    """Read a JSON mapping, returning empty dict on absence/corruption."""
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_file(path: Path, data: dict[str, Any]) -> None:
    """Atomically write a JSON mapping with 0600 permissions."""
    _ensure_dir(path.parent)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.chmod(tmp_path, FILE_MODE)
    os.replace(tmp_path, path)
    os.chmod(path, FILE_MODE)


def _write_text(path: Path, content: str) -> None:
    """Atomically write text with 0600 permissions."""
    _ensure_dir(path.parent)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(tmp_path, FILE_MODE)
    os.replace(tmp_path, path)
    os.chmod(path, FILE_MODE)


def _run_mechanical_command(command: str, cwd: str, timeout_seconds: int) -> tuple[str, int]:
    """Run one mechanical command."""
    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return f"{stdout}{stderr}\ncommand timed out", 124
    output = f"{proc.stdout or ''}{proc.stderr or ''}"
    return output, proc.returncode


def _load_failure_detector() -> Any:
    """Load core failure_detector lazily."""
    candidates = []
    orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
    if orchestra_dir:
        candidates.append(Path(orchestra_dir) / "packages" / "core" / "hooks")
    candidates.append(Path(__file__).resolve().parents[2] / "core" / "hooks")
    for path in candidates:
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import failure_detector

    return failure_detector


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSONL record with flock and 0600 permissions."""
    _ensure_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, FILE_MODE)
    os.chmod(path, FILE_MODE)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _loads_json_line(line: str) -> dict[str, Any]:
    """Parse one JSONL line safely."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _ensure_dir(path: Path) -> None:
    """Create directory and enforce owner-only permissions."""
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path, DIR_MODE)


def _read_lock(path: Path) -> LockInfo | None:
    """Read lock.json."""
    data = _read_json_file(path)
    if not data:
        return None
    try:
        return LockInfo(
            owner_id=str(data["owner_id"]),
            pid=int(data["pid"]),
            host=str(data["host"]),
            started_at=str(data["started_at"]),
            heartbeat_at=str(data["heartbeat_at"]),
            ttl=int(data["ttl"]),
            lease_token=str(data["lease_token"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _acquire_existing_lock(
    path: Path, owner_id: str, ttl_seconds: int, host: str
) -> LockInfo | None:
    """Acquire an existing stale lock or reject live locks."""
    fd = _open_lock_for_update(path)
    if fd is None:
        return None
    try:
        with os.fdopen(fd, "r+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            if not _fd_matches_path(f.fileno(), path):
                return None
            return _rewrite_stale_lock_stream(f, owner_id, ttl_seconds, host)
    except OSError:
        return None


def _rewrite_stale_lock_stream(
    stream: Any, owner_id: str, ttl_seconds: int, host: str
) -> LockInfo | None:
    """Rewrite an already-locked stale lock stream with a fresh lease."""
    lock = _read_lock_stream(stream)
    if _live_lock_blocks_acquire(lock, host):
        return None
    new_lock = _new_lock(owner_id, ttl_seconds, host)
    _rewrite_locked_file(stream, asdict(new_lock))
    return new_lock


def _live_lock_blocks_acquire(lock: LockInfo | None, host: str) -> bool:
    """Return True for live same-host locks; raise for live foreign-host locks."""
    if lock is None or not is_lease_alive(lock):
        return False
    if lock.host != host:
        raise ForeignLeaseError(lock)
    return True


def _replace_lock(
    loop_id: str,
    project_dir: str,
    owner_id: str,
    ttl_seconds: int,
    host: str | None,
) -> None:
    """Replace lock.json with a fresh lease without validating the old token."""
    path = lock_path(loop_id, project_dir)
    _ensure_dir(path.parent)
    lock = _new_lock(owner_id, ttl_seconds, host or socket.gethostname())
    _write_json_file(path, asdict(lock))


def _write_new_lock_fd(fd: int, owner_id: str, ttl_seconds: int, host: str) -> LockInfo:
    """Write a new LockInfo to an already-open file descriptor."""
    lock = _new_lock(owner_id, ttl_seconds, host)
    content = json.dumps(asdict(lock), ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        os.fchmod(fd, FILE_MODE)
        written = os.write(fd, content)
        if written != len(content):
            raise OSError("short lock write")
        os.fsync(fd)
    finally:
        os.close(fd)
    return lock


def _new_lock(owner_id: str, ttl_seconds: int, host: str) -> LockInfo:
    """Create a new lock object."""
    now = now_iso()
    return LockInfo(owner_id, os.getpid(), host, now, now, ttl_seconds, secrets.token_hex(16))


def _parse_epoch(value: str) -> float | None:
    """Parse ISO8601 timestamp to epoch seconds."""
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def resolve_root_worktree(project_dir: str) -> Path:
    """Resolve and cache the main worktree root, failing closed on git errors."""
    key = str(Path(project_dir).resolve())
    if key not in _ROOT_CACHE:
        _ROOT_CACHE[key] = _resolve_root_worktree(project_dir)
    return _ROOT_CACHE[key]


def _resolve_root_worktree(project_dir: str) -> Path:
    """Resolve main worktree root through git-common-dir."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RootResolutionError(f"could not resolve root worktree: {project_dir}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise RootResolutionError(f"could not resolve root worktree: {project_dir}")
    root = Path(result.stdout.strip()).parent
    if not root.is_absolute() or not root.is_dir():
        raise RootResolutionError(f"invalid root worktree: {root}")
    return root


def _validate_safe_id(field: str, value: str) -> None:
    """Reject path separators and unsafe ids for path components."""
    if not _SAFE_ID_RE.match(value):
        raise ValueError(f"Unsafe {field}: {value}")
    if ".." in value:
        raise ValueError(f"Unsafe {field}: {value}")


def _foreign_host_stop_result(loop_id: str, project_dir: str, lock: LockInfo) -> ProposeResult:
    """Persist or replay a pending stop proposal for a live foreign-host lease."""
    state = load_state(loop_id, project_dir)
    params = {"stop_reason": "foreign_live_lease", "foreign_host": lock.host}
    pending = state.pending_action
    if (
        pending is not None
        and pending.action == Action.STOP.value
        and state.stop_reason == "foreign_live_lease"
    ):
        return ProposeResult(
            action=pending.action,
            action_id=pending.action_id,
            state_version=state.state_version,
            expected_phase=pending.phase,
            phase=pending.phase,
            iteration=pending.iteration,
            context=_proposal_context(params),
        )
    action_id = f"act-{secrets.token_hex(ACTION_ID_BYTES)}"
    iteration = _next_action_iteration(state, Action.STOP.value)
    state.status = "stopped"
    state.stop_reason = "foreign_live_lease"
    state.pending_action = PendingAction(
        action_id, Action.STOP.value, state.phase, iteration, now_iso()
    )
    state.state_version += 1
    state.updated_at = now_iso()
    append_journal_event(loop_id, project_dir, "stopped", "step", None, params)
    append_journal_event(
        loop_id,
        project_dir,
        "pending",
        "step",
        action_id,
        {
            "action": Action.STOP.value,
            "expected_phase": state.phase,
            "replaced_action_id": pending.action_id if pending is not None else None,
        },
    )
    _write_state(state, project_dir)
    return ProposeResult(
        action=Action.STOP.value,
        action_id=action_id,
        state_version=state.state_version,
        expected_phase=state.phase,
        phase=state.phase,
        iteration=iteration,
        context=_proposal_context(params),
    )


def _open_lock_for_update(path: Path) -> int | None:
    """Open lock file for locked in-place updates."""
    try:
        return os.open(path, os.O_RDWR)
    except OSError:
        return None


def _fd_matches_path(fd: int, path: Path) -> bool:
    """Return True if fd still points at the current path inode."""
    try:
        fd_stat = os.fstat(fd)
        path_stat = os.stat(path)
    except OSError:
        return False
    return fd_stat.st_ino == path_stat.st_ino and fd_stat.st_dev == path_stat.st_dev


def _read_lock_stream(stream: Any) -> LockInfo | None:
    """Read LockInfo from an already-open stream."""
    try:
        stream.seek(0)
        data = json.load(stream)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return LockInfo(
            owner_id=str(data["owner_id"]),
            pid=int(data["pid"]),
            host=str(data["host"]),
            started_at=str(data["started_at"]),
            heartbeat_at=str(data["heartbeat_at"]),
            ttl=int(data["ttl"]),
            lease_token=str(data["lease_token"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _rewrite_locked_file(stream: Any, data: dict[str, Any]) -> None:
    """Rewrite an already-locked file in place."""
    stream.seek(0)
    stream.truncate()
    json.dump(data, stream, ensure_ascii=False, sort_keys=True)
    stream.flush()
    os.fsync(stream.fileno())
    os.fchmod(stream.fileno(), FILE_MODE)
