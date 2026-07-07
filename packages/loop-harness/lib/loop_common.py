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
        r"\b[A-Za-z0-9_-]{0,20}(api[_-]?key|token|password|secret|credential)\b\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\b(AKIA|ASIA|A3T)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
    re.compile(r"\bSharedAccessSignature\s*=\s*\S+", re.IGNORECASE),
    re.compile(r"-----BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
]

_FAILED_NODEID_RE = re.compile(r"^FAILED\s+(\S+?)(?:\s+-\s+.*)?$", re.MULTILINE)
_RUFF_RULE_RE = re.compile(r"^\S+:\d+:\d+:\s+([A-Z]{1,4}\d{2,4})\b", re.MULTILINE)


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
        return {str(key): redact_payload(item) for key, item in value.items()}
    return value


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
) -> ProposeResult:
    """Create initial state, acquire a lease, and return the first proposal."""
    if state_path(loop_id, project_dir).exists():
        raise InvalidStateError(f"state already exists: {loop_id}; use attach or resume")
    lock = acquire_lock(loop_id, project_dir, owner_id, ttl_seconds, host)
    if lock is None:
        raise ForeignLeaseError(None)
    state = _initial_state(loop_id, definition_id, repo_identity_hash, worktree_path, branch, phase)
    append_journal_event(loop_id, project_dir, "loop_created", "step", None, asdict(state))
    _write_state(state, project_dir)
    result = propose(loop_id, project_dir, lock.lease_token)
    return _with_context(result, {"lease_token": lock.lease_token})


def propose(
    loop_id: str, project_dir: str, lease_token: str, recover_orphans: bool = False
) -> ProposeResult:
    """Reconcile first, then create exactly one pending action."""
    foreign = check_foreign_host(loop_id, project_dir)
    if foreign is not None:
        return _foreign_host_stop_result(loop_id, project_dir, foreign)
    _ensure_valid_lease(loop_id, project_dir, lease_token)
    reconcile(loop_id, project_dir, lease_token, allow_side_effect_resolution=recover_orphans)
    state = load_state(loop_id, project_dir)
    if state.pending_action is not None:
        raise ProtocolViolationError("pending action must be completed before propose")
    action = _next_action(state, project_dir)
    action_id = f"act-{secrets.token_hex(ACTION_ID_BYTES)}"
    iteration = _next_action_iteration(state, action)
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
        action, action_id, new_state.state_version, state.phase, state.phase, iteration
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
        return CompleteResult(
            True, True, state.last_completed_action.state_version_after, "call propose again"
        )
    if _is_stale_complete(state, action_id, state_version):
        raise StaleActionError(f"stale action: {action_id}")

    assert state.pending_action is not None
    action = state.pending_action.action
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
    return CompleteResult(True, False, new_version, "call propose again")


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
        state.status = "waiting_external" if not result.get("completed") else "running"
        return
    if action == Action.STOP.value:
        state.status = "stopped"
        state.stop_reason = str(result.get("stop_reason") or "safety_stop")


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
    _update_no_progress(counters, phase_check.signature)
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


def run_mechanical_checks(
    commands: list[str], cwd: str, timeout_seconds: int
) -> list[MechanicalFailure]:
    """Run mechanical checker commands and classify failures via failure_detector."""
    detector = _load_failure_detector()
    failures: list[MechanicalFailure] = []
    for command in commands:
        output, exit_code = _run_mechanical_command(command, cwd, timeout_seconds)
        response = {"exit_code": exit_code, "stdout": output}
        result = detector.analyze("Bash", {"command": command}, response)
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
        result.action,
        result.action_id,
        result.state_version,
        result.expected_phase,
        result.phase,
        result.iteration,
        context,
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
    check_result = _extract_check_result_dict(result)
    if check_result is not None:
        payload["check_result"] = check_result
    return payload


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
    last_action = _last_completed_action_name(state.loop_id, project_dir, state)
    return (
        Action.RUN_CHECKER.value
        if last_action == Action.RUN_MAKER.value
        else Action.RUN_MAKER.value
    )


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


def _apply_safety_stop_if_needed(state: LoopState, action: str, result: dict[str, Any]) -> bool:
    """Apply safety stop for push guard violations."""
    guard = result.get("push_guard")
    if action != Action.ADVANCE_PHASE.value or not isinstance(guard, dict):
        return False
    if guard.get("branch_ok", True) and guard.get("repo_identity_ok", True):
        return False
    state.status = "stopped"
    state.stop_reason = str(guard.get("reason") or "push_guard_failed")
    return True


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
    decision = evaluate_guards(state, phase_check, result.get("phase_def"), result.get("config"))
    if decision.disposition == "continue" or decision.disposition == "retry":
        state.status = "running"
        return
    if decision.disposition == Action.ADVANCE_PHASE.value:
        state.phase = decision.next_phase or state.phase
        state.guards.setdefault(state.phase, GuardCounters())
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
    return _finalize_reconciled(loop_id, project_dir, state, source)


def _reconcile_from_artifact(
    loop_id: str, project_dir: str, state: LoopState, action_id: str, artifact: str
) -> ReconcileOutcome:
    """Resolve pending checker action from check_result.json artifact."""
    try:
        result = json.loads(artifact)
    except json.JSONDecodeError as exc:
        raise IntegrityError("invalid check_result artifact") from exc
    apply_action_effect(state, Action.RUN_CHECKER.value, result, project_dir, loop_id, action_id)
    return _finalize_reconciled(loop_id, project_dir, state, "artifact")


def _finalize_reconciled(
    loop_id: str, project_dir: str, state: LoopState, source: str
) -> ReconcileOutcome:
    """Append reconciled journal event and write state."""
    action_id = state.pending_action.action_id if state.pending_action else None
    state.pending_action = None
    state.state_version += 1
    state.updated_at = now_iso()
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


def _update_no_progress(counters: GuardCounters, signature: str) -> None:
    """Update no-progress counters."""
    if signature == counters.last_signature:
        counters.no_progress_streak += 1
        return
    counters.no_progress_streak = 1
    counters.last_signature = signature


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
    return {
        "passed": result.passed,
        "signature": result.signature,
        "infrastructure_failure": result.infrastructure_failure,
        "results": [check_result_to_dict(item) for item in result.results],
    }


def phase_check_from_dict(data: dict[str, Any]) -> PhaseCheckResult:
    """Deserialize PhaseCheckResult from JSON-like dict."""
    return PhaseCheckResult(
        passed=bool(data.get("passed")),
        results=[check_result_from_dict(item) for item in data.get("results") or []],
        signature=str(data.get("signature") or ""),
        infrastructure_failure=bool(data.get("infrastructure_failure")),
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
    """Write text with 0600 permissions."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
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
    snapshot = _read_lock(path)
    if snapshot is not None and is_lease_alive(snapshot):
        if snapshot.host != host:
            raise ForeignLeaseError(snapshot)
        return None
    try:
        if _read_lock(path) != snapshot:
            return None
        path.unlink()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    except (OSError, FileExistsError):
        return None
    return _write_new_lock_fd(fd, owner_id, ttl_seconds, host)


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
    """Return a stop proposal for a live foreign-host lease."""
    state = load_state(loop_id, project_dir)
    action_id = f"act-{secrets.token_hex(ACTION_ID_BYTES)}"
    context = {"stop_reason": "foreign_live_lease", "foreign_host": lock.host}
    return ProposeResult(
        Action.STOP.value,
        action_id,
        state.state_version,
        state.phase,
        state.phase,
        state.iteration,
        context,
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
