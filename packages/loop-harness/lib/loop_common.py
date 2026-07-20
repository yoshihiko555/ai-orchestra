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
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
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
# Marker embedded in a retired `pending` loop's state-dir name (#H3/#H11, see
# `loop_scheduler.recover_orphaned_pending_loops`). Real loop_ids are always
# `<repo-identity-hash>-issue-<N>` (see `worktree_manager.compute_loop_id`), which never
# contains this substring, so filtering on it cannot collide with a live loop_id. Shared
# between `loop_scheduler.py` (writes it) and `loop_status.py` (must treat it specially for
# both display and purge eligibility, SM1) so both stay in sync on one literal.
ORPHANED_PENDING_MARKER = ".orphaned-"
_ROOT_CACHE: dict[str, Path] = {}
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_RESERVED_PROPOSAL_CONTEXT_KEYS = frozenset({"lease_token", "params", "reason"})
_PUBLIC_STOP_REASONS = frozenset(
    {
        "safety_stop",
        "push_guard_violation",
        "repo_identity_mismatch",
        "foreign_live_lease",
        "external_reviewer_unavailable",
        "git_ref_import_failed",
        "git_ref_not_fast_forward",
        "git_ref_cas_rejected",
    }
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
    maker_agent: str | None = None
    remote_head_baseline: str | None = None


#: Severities that block phase progress (pr_review_response no-progress guard, Maker prompt
#: input filtering). Medium/low findings are reported but never block or feed the guard.
BLOCKING_SEVERITIES = frozenset({"critical", "high"})


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


def coord_lock_path(loop_id: str, project_dir: str) -> Path:
    """Return the fixed, purge-independent per-loop coordination lock path (SN-flock).

    Unlike `lock_path` (`.claude/loop/<loop_id>/lock.json`, deleted together with the rest of
    the per-loop directory by `loop_status.purge_loop`'s `rmtree`), this path lives directly
    under `.claude/loop/` so its inode stays stable across a purge. A flock taken on
    `lock.json` itself (the previous purge-vs-resume/attach guard) stops protecting anything
    the moment `rmtree` removes that inode: a concurrent `resume`/`reacquire_lease` call
    racing the purge would recreate a brand-new `lock.json` (a different inode) and never
    contend with a flock still held on the now-deleted one. `purge`, `resume`, and the
    scheduler's repo-identity safety-stop all acquire this same, never-deleted path instead
    (via `held_coord_lock`), so they always contend on one stable file regardless of any
    `lock.json`/state-dir churn happening underneath.
    """
    _validate_safe_id("loop_id", loop_id)
    return loop_root(project_dir) / f"{loop_id}.coord.lock"


@contextmanager
def held_coord_lock(loop_id: str, project_dir: str) -> Iterator[None]:
    """Acquire the per-loop coordination lock (SN-flock) for the duration of the block.

    Creates the lock file if absent (`O_CREAT`); its content carries no meaning beyond being
    a stable flock target, and it is never deleted (unlike `lock_path`'s lock.json, which
    `purge_loop`'s `rmtree` removes) so every caller always flocks the same inode. Nests
    safely with any lock taken on a *different* path (e.g. `lock.json`'s own flock inside
    `resume`/`_purge_if_still_safe`) since `flock()` contention is per-file, not per-process.
    """
    path = coord_lock_path(loop_id, project_dir)
    _ensure_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, FILE_MODE)
    with os.fdopen(fd, "r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield


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
    *,
    precedent_push_check: bool = False,
) -> ProposeResult:
    """Create initial state, acquire a lease, and return the first proposal.

    `precedent_push_check` (Issue #196, default `False` -- see `propose()`): threaded through
    to the initial `propose()` call this makes. Both LP-1 (`loop_step.py`) and LP-2
    (`loop_driver.py`) call `start()`, so the default must stay `False` (LP-2 already has its
    own, separate, more rigorous push-integrity mechanism, `loop_driver_support`'s
    `get_remote_head()`/`classify_push_integrity()`) -- only `loop_step.py`'s own call site
    opts in.
    """
    if state_path(loop_id, project_dir).exists():
        raise InvalidStateError(f"state already exists: {loop_id}; use attach or resume")
    if preacquired_lock is None:
        lock = acquire_lock(loop_id, project_dir, owner_id, ttl_seconds, host)
        if lock is None:
            raise ForeignLeaseError(None)
    else:
        lock = preacquired_lock
        _ensure_valid_lease(loop_id, project_dir, lock.lease_token)
    state = _initial_state(
        loop_id,
        definition_id,
        repo_identity_hash,
        worktree_path,
        branch,
        phase,
        precedent_push_check=precedent_push_check,
    )
    # `_drop_none_remote_head_baseline()`: keep the journal payload byte-for-byte identical to
    # the pre-Issue #196 shape for non-opted-in callers (bot review, PR #277, Codex P2) -- see
    # its docstring for the full contract.
    journal_payload = _drop_none_remote_head_baseline(asdict(state))
    append_journal_event(loop_id, project_dir, "loop_created", "step", None, journal_payload)
    _write_state(state, project_dir)
    result = propose(
        loop_id, project_dir, lock.lease_token, precedent_push_check=precedent_push_check
    )
    return _with_context(result, {"lease_token": lock.lease_token})


def propose(
    loop_id: str,
    project_dir: str,
    lease_token: str,
    recover_orphans: bool = False,
    *,
    precedent_push_check: bool = False,
) -> ProposeResult:
    """Reconcile first, then create exactly one pending action.

    `precedent_push_check` (Issue #196, default `False`): both LP-1 (`loop_step.py`) and LP-2
    (`loop_driver.py`) call this function, but LP-2 already has its own, separate, more
    rigorous push-integrity mechanism (`loop_driver_support`'s `get_remote_head()`/
    `classify_push_integrity()`, run around the actual push rather than at proposal time).
    Defaulting to `False` keeps LP-2 (and any other existing caller) byte-for-byte unaffected
    -- no extra `git ls-remote` network round trip, no new `push_integrity_warning` journal
    event -- so only `loop_step.py`'s own LP-1 call sites, which explicitly pass `True`, get
    this best-effort warning-only fallback (`_detect_precedent_push()`).
    """
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
    # Issue #196 (LP-1 push-integrity warning, best-effort, opt-in via `precedent_push_check`):
    # only a proposal that is actually about to push (`ADVANCE_PHASE`), re-check a push's
    # external review (`WAIT_EXTERNAL_REVIEW`), or exit with a possible draft-PR push
    # (`EXIT_FAILURE`, whose `on_failure.exec` can itself push -- see `_proposal_params()`) is a
    # meaningful point to look for drift; `action` has already been forced to `STOP` above by
    # the existing branch/repo-identity safety-stop checks when those tripped, so this never
    # runs redundantly alongside a hard stop for a *different* reason. `RUN_MAKER`/`RUN_CHECKER`
    # (the hot, per-iteration path) are deliberately excluded -- checking only at the less
    # frequent, push-adjacent actions keeps this from adding a network round trip to every
    # single propose() call.
    precedent_push_head = None
    if precedent_push_check and action in {
        Action.ADVANCE_PHASE.value,
        Action.WAIT_EXTERNAL_REVIEW.value,
        Action.EXIT_FAILURE.value,
    }:
        precedent_push_head = _detect_precedent_push(state)
    iteration = _next_action_iteration(state, action)
    params = _proposal_params(state, action, project_dir)
    new_state = copy.deepcopy(state)
    new_state.pending_action = PendingAction(action_id, action, state.phase, iteration, now_iso())
    new_state.state_version += 1
    new_state.updated_at = now_iso()
    # DH1: validate the lease and that `state` is still current *inside* one held flock
    # section immediately around the write, closing the TOCTOU window between the
    # `_ensure_valid_lease` check above and this write (a lease can expire and be
    # reacquired by another worker in that gap otherwise).
    with guarded_lease_section(loop_id, project_dir, lease_token):
        _ensure_unchanged_since(loop_id, project_dir, state.state_version)
        if precedent_push_head is not None:
            # Warning only (see `_detect_precedent_push()`): never blocks this proposal or
            # changes `state.status`, only leaves an audit trail for the human driving LP-1.
            append_journal_event(
                loop_id,
                project_dir,
                "push_integrity_warning",
                "step",
                action_id,
                {
                    "action": action,
                    "expected_head": state.remote_head_baseline,
                    "observed_head": precedent_push_head,
                },
            )
        append_journal_event(
            loop_id,
            project_dir,
            "pending",
            "step",
            action_id,
            {"action": action, "expected_phase": state.phase},
        )
        _write_state(new_state, project_dir)
    context = _proposal_context(params)
    if precedent_push_head is not None:
        # Issue #196: surface the same warning in the proposal JSON itself, not only the
        # journal file -- an LP-1 operator's primary signal is `loop_step.py propose`'s own
        # response, which they read every cycle; a journal-only record would go unnoticed
        # unless someone happens to be tailing `journal.jsonl` at the same time.
        context = {
            **context,
            "push_integrity_warning": {
                "expected_head": state.remote_head_baseline,
                "observed_head": precedent_push_head,
            },
        }
    return ProposeResult(
        action=action,
        action_id=action_id,
        state_version=new_state.state_version,
        expected_phase=state.phase,
        phase=state.phase,
        iteration=iteration,
        context=context,
    )


def complete(
    loop_id: str,
    project_dir: str,
    action_id: str,
    state_version: int,
    result: dict[str, Any],
    lease_token: str,
    *,
    precedent_push_check: bool = False,
) -> CompleteResult:
    """Complete the pending action using journal-first ordering.

    `precedent_push_check` (Issue #196, default `False`): see `propose()`'s own docstring for
    why this defaults off (LP-2 shares this function and already has its own push-integrity
    mechanism) -- threaded through to `apply_action_effect()`'s `remote_head_baseline` refresh.
    """
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
    selected_maker_agent = None
    if action == Action.RUN_MAKER.value and state.definition_id == "issue-loop":
        selected_maker_agent = _selected_maker_from_result(state, result, project_dir)
    if action == Action.RUN_CHECKER.value:
        validate_implementation_checker_result(state, result, project_dir)
    new_version = state.state_version + 1
    # Issue #196 follow-up (PR review high-severity fix): `apply_action_effect()`'s
    # `remote_head_baseline` refresh is network I/O (`_remote_head()`'s `git ls-remote`, up to
    # `REMOTE_LS_TIMEOUT_SECONDS`). Query it here, *before* acquiring the exclusive lease flock
    # below, so a slow/hanging remote cannot hold up every other flock-holding operation (lease
    # renewal, heartbeat, `loop_status.py` purge, a concurrent `resume()`) for as long as that
    # query takes on every `advance_phase`/qualifying `wait_external_review`/`exit_failure`
    # completion. Mirrors `propose()`'s own `_detect_precedent_push()` call, which likewise runs
    # outside its lock. Computed *before* `_completed_payload()` (Issue #196 PR review round 2,
    # "Persist remote head for crash replay") so the observed head can be baked into the very same
    # `completed` journal event `_reconcile_from_payload()` replays from -- a crash between that
    # event and `_write_state()` below would otherwise leave recovery with no way to restore this
    # baseline for a legitimate push, and the next proposal could emit a false
    # `push_integrity_warning` or lose the ability to distinguish a later real one.
    remote_head_refresh = _precompute_remote_head_refresh(
        state, action, loop_id, project_dir, precedent_push_check=precedent_push_check
    )
    payload = _completed_payload(action, result, remote_head_refresh)
    # DH1: see `propose()` for why validation and the write must share one held flock.
    with guarded_lease_section(loop_id, project_dir, lease_token):
        _ensure_unchanged_since(loop_id, project_dir, state.state_version)
        append_journal_event(
            loop_id, project_dir, "completed", _actor_for(action), action_id, payload
        )
        apply_action_effect(
            state,
            action,
            result,
            project_dir,
            loop_id,
            action_id,
            selected_maker_agent=selected_maker_agent,
            precedent_push_check=precedent_push_check,
            remote_head_refresh=remote_head_refresh,
        )
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
        return _reconcile_from_payload(
            loop_id, project_dir, state, completed, "journal", lease_token
        )
    artifact = load_artifact(loop_id, project_dir, pending.action_id, "check_result.json")
    if artifact is not None and pending.action == Action.RUN_CHECKER.value:
        return _reconcile_from_artifact(
            loop_id, project_dir, state, pending.action_id, artifact, lease_token
        )
    if pending.action == Action.RUN_CHECKER.value:
        return ReconcileOutcome("rerun_required", state.state_version)
    if not allow_side_effect_resolution:
        return ReconcileOutcome("unresolved_pending", state.state_version)
    return _mark_unresolved_pending(loop_id, project_dir, state, lease_token)


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
    """Resume a failed/stopped loop and issue a new lease.

    SN-flock: the reload-through-write section is held under `held_coord_lock` (a fixed,
    purge-independent path) so a concurrent `loop_status.py purge` of this same loop_id
    cannot race this call - either this call blocks until the purge finishes (and then
    correctly fails with `InvalidStateError` once `load_state` finds nothing left to resume),
    or the purge blocks until this call's write completes (and then finds the loop no longer
    purge-eligible on its own post-lock reload).
    """
    if not reset_counters:
        raise InvalidStateError("resume requires reset_counters=True")
    with held_coord_lock(loop_id, project_dir):
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
    """Reacquire a stale lease for pending/running/waiting_external loops, then propose.

    `pending` is accepted (Issue #205): a caller that crashes between `start`'s initial
    `run_maker` proposal and its `complete` call otherwise has no recovery entry point,
    since `resume` only handles `failed`/`stopped`. `propose(recover_orphans=True)`'s
    existing reconcile path (`_mark_unresolved_pending`) already treats an orphaned
    side-effectful pending action as an infrastructure failure and re-proposes (or fails
    via guard exhaustion) independent of `state.status`, so this only widens which
    statuses may reach it. Note `loop_scheduler.py` deliberately continues to exclude
    `pending` from automatic discovery/respawn (#G10) even though this function can now
    recover it - only an explicit, manual (or LP-1-driven) `attach` call does.
    """
    state = load_state(loop_id, project_dir)
    if state.status not in {"pending", "running", "waiting_external"}:
        raise InvalidStateError(f"cannot attach status={state.status}")
    lock = reacquire_lease(loop_id, project_dir, owner_id, ttl_seconds)
    result = propose(loop_id, project_dir, lock.lease_token, recover_orphans=True)
    return _with_context(result, {"lease_token": lock.lease_token})


class _NotPrecomputed:
    """Sentinel marking `remote_head_refresh` as not supplied by the caller (Issue #196 follow-up,
    PR review high-severity fix).

    Distinguishes "the caller wants `_remote_head()` queried now" (this sentinel, the default for
    direct/backward-compatible callers of `apply_action_effect()`/`_apply_advance_phase()`) from
    "the caller already queried it and is passing the resolved value" (`None`, meaning no refresh
    applies or the query was unverifiable, or a real sha/`LP1_REMOTE_HEAD_ABSENT` string) -- plain
    `None` on its own would be ambiguous between "not computed yet" and "computed, nothing to
    apply".
    """


_REMOTE_HEAD_NOT_PRECOMPUTED = _NotPrecomputed()


def apply_action_effect(
    state: LoopState,
    action: str,
    result: dict[str, Any],
    project_dir: str | None = None,
    loop_id: str | None = None,
    action_id: str | None = None,
    selected_maker_agent: str | None = None,
    allow_legacy_maker_result: bool = False,
    *,
    precedent_push_check: bool = False,
    remote_head_refresh: str | None | _NotPrecomputed = _REMOTE_HEAD_NOT_PRECOMPUTED,
) -> None:
    """Apply a completed action to state.

    `precedent_push_check` (Issue #196, default `False`): see `propose()`'s own docstring for
    why this defaults off (LP-2 shares this function via `complete()` and already has its own
    push-integrity mechanism) -- gates whether `ADVANCE_PHASE`/`WAIT_EXTERNAL_REVIEW` refresh
    `state.remote_head_baseline` at all.

    `remote_head_refresh` (Issue #196 follow-up, PR review high-severity fix): the already-
    observed remote HEAD to seed as the new baseline, or `None` when no refresh should happen.
    Defaults to the `_REMOTE_HEAD_NOT_PRECOMPUTED` sentinel for direct/backward-compatible
    callers (e.g. unit tests invoking this function standalone), which falls back to querying
    `_remote_head()` right here. `complete()` never hits that fallback: it precomputes the query
    result *before* acquiring its `guarded_lease_section` flock and always passes the resolved
    value through, so this function performs no network I/O while that lock is held.
    """
    if _apply_safety_stop_if_needed(state, action, result):
        return
    if action == Action.RUN_MAKER.value:
        if state.definition_id == "issue-loop":
            _persist_selected_maker(
                state,
                result,
                project_dir,
                selected_maker_agent,
                allow_legacy_maker_result,
            )
        if result.get("infrastructure_failure"):
            # I5 (PR #210 review round 5): a Maker that times out or exits non-zero
            # (`_run_maker`'s `infrastructure_failure: True` result) used to always complete
            # as `status="running"` here, unconditionally treated as a normal run_maker
            # success — `evaluate_guards()`'s infra-retry counter was only ever reachable via
            # `_apply_checker_result` (RUN_CHECKER/WAIT_EXTERNAL_REVIEW), never RUN_MAKER, so
            # repeated Maker infra failures never counted toward `guards.infrastructure_failure.
            # max_retries` and could not turn into a real loop failure.
            _apply_maker_infrastructure_failure(state, project_dir)
            return
        state.status = "running"
        return
    if action == Action.RUN_CHECKER.value:
        _apply_checker_result(state, result, project_dir, loop_id, action_id)
        return
    if action == Action.WAIT_EXTERNAL_REVIEW.value:
        # Issue #196 (LP-1 push-integrity warning): `wait_external_review` itself carries a
        # push when it directly follows `run_maker` (`push_required`, `_proposal_params()`) --
        # e.g. addressing PR review comments before waiting on the next review round. Refresh
        # the baseline here too (not only in `_apply_advance_phase`), or that legitimate push
        # would itself look like drift at the *next* `propose()` call's `_detect_precedent_push`
        # check (a false positive warning).
        _apply_remote_head_refresh(
            state,
            should_refresh=precedent_push_check
            and _wait_external_review_had_required_push(state, loop_id, project_dir),
            remote_head_refresh=remote_head_refresh,
        )
        if _extract_check_result_payload(result) is not None:
            _apply_checker_result(state, result, project_dir, loop_id, action_id)
            return
        state.status = "waiting_external" if not result.get("completed") else "running"
        return
    if action == Action.ADVANCE_PHASE.value:
        _apply_advance_phase(
            state,
            result,
            precedent_push_check=precedent_push_check,
            remote_head_refresh=remote_head_refresh,
        )
        return
    if action == Action.EXIT_FAILURE.value:
        # Issue #196 PR review round 2 ("Refresh after failure-exit draft pushes"): an
        # `exit_failure` completion whose phase's `on_failure.exec` pushed a Draft PR branch
        # (`draft_pr_exec`, `_proposal_params()`) is itself a legitimate push, exactly like the
        # `ADVANCE_PHASE`/`WAIT_EXTERNAL_REVIEW` branches above -- `resume()` explicitly allows
        # resuming a `failed` loop back to `running`, so leaving the pre-push baseline stale here
        # would make that same push look like Maker drift at the *next* `propose()` call's
        # `_detect_precedent_push` check (a false positive blaming the loop's own prior
        # failure-exit push instead of a real out-of-band one).
        _apply_remote_head_refresh(
            state,
            should_refresh=precedent_push_check and _exit_failure_requires_push(state, project_dir),
            remote_head_refresh=remote_head_refresh,
        )
        return
    if action == Action.STOP.value:
        state.status = "stopped"
        state.stop_reason = str(result.get("stop_reason") or state.stop_reason or "safety_stop")


def _apply_maker_infrastructure_failure(state: LoopState, project_dir: str | None) -> None:
    """Guard-count (and possibly fail) a Maker infra failure (I5, PR #210 review round 5).

    Mirrors `_apply_checker_result`'s infrastructure-failure handling (and
    `_mark_unresolved_pending`'s same `PhaseCheckResult(False, [], ..., True)` ->
    `evaluate_guards()` pattern) so a Maker that repeatedly times out or exits non-zero
    increments `state.guards[phase].infrastructure_failure_count` and, once
    `guards.infrastructure_failure.max_retries` is reached, is converted into a real
    `on_failure.disposition` outcome — instead of silently completing `run_maker` as
    `status="running"` forever regardless of how many consecutive infra failures occurred.

    `phase_check.passed` is always `False` here (a Maker infra failure is never itself a
    "success" signal), so `evaluate_guards()`'s success branch (`Action.ADVANCE_PHASE.value`/
    `Action.EXIT_SUCCESS.value`) can never be reached from this call site; only
    `"continue"`/`"retry"` (not yet exhausted), `Action.STOP.value`, or the phase's configured
    `on_failure.disposition` (commonly `Action.EXIT_FAILURE.value`) are possible outcomes.
    """
    phase_def = _load_phase_definition(state, project_dir) if project_dir else None
    config = _load_loop_config(project_dir) if project_dir else DEFAULT_CONFIG
    phase_check = PhaseCheckResult(False, [], "maker_infrastructure_failure", True)
    decision = evaluate_guards(state, phase_check, phase_def, config)
    if decision.disposition in ("continue", "retry"):
        state.status = "running"
        return
    if decision.disposition == Action.STOP.value:
        state.status = "stopped"
        state.stop_reason = decision.reason or "safety_stop"
        return
    state.status = "failed"
    state.stop_reason = decision.reason or "guard_failed"


def evaluate_guards(
    state: LoopState,
    phase_check: PhaseCheckResult,
    phase_def: Any | None,
    config: dict[str, Any] | None,
) -> GuardDecision:
    """Evaluate infra failure, pass, no-progress, then iteration limit."""
    if (
        phase_check.signature == "external_reviewer_unavailable"
        and phase_check.metadata.get("reviewer_unavailable_reason") == "rate_limited"
    ):
        return GuardDecision(Action.STOP.value, "external_reviewer_unavailable")
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


def build_pr_iteration_findings(
    pr_review: dict[str, Any], iteration: int, *, severities: frozenset[str] | None = None
) -> IterationFindings:
    """Build current-iteration open signature summary from state.pr_review.

    When `severities` is given, only records whose `severity` is a member contribute to
    `signatures`/`new_count` (dismissed records are always excluded regardless of severity).
    Callers feeding the no-progress guard pass `severities=BLOCKING_SEVERITIES` so low/medium
    findings can never contribute to a no-progress determination (issue #213).
    """
    signatures: set[str] = set()
    new_count = 0
    for signature, record in _pr_findings_map(pr_review).items():
        if record.get("status") == "dismissed":
            continue
        if severities is not None and record.get("severity") not in severities:
            continue
        if int(record.get("last_seen_iteration") or 0) == iteration:
            signatures.add(signature)
        if int(record.get("first_seen_iteration") or 0) == iteration:
            new_count += 1
    return IterationFindings(frozenset(signatures), new_count)


def evaluate_pr_review_no_progress(
    previous: IterationFindings, current: IterationFindings
) -> NoProgressResult:
    """Evaluate PR review no-progress by exact re-raise of the same blocking signature set.

    Callers must pass severity-filtered `IterationFindings` (`severities=BLOCKING_SEVERITIES`,
    see `build_pr_iteration_findings`) so this only ever compares critical/high signatures.
    Only an identical, non-empty blocking signature set re-raised across iterations counts as
    no-progress. A completely new signature set, or any partial reduction of the previous set
    (e.g. `{A, B}` -> `{A, C}` or `{A, B}` -> `{A}`), counts as progress: the Maker made *some*
    change even if it didn't fully resolve every finding. Runaway iteration without full
    convergence is bounded separately by the `max_iterations` and `no_new_commit` guards, not
    by this signature comparison (issue #213).
    """
    if current.signatures and current.signatures == previous.signatures:
        return NoProgressResult(True, "reraised", current.signatures)
    return NoProgressResult(False, "progress")


def run_mechanical_checks(
    commands: list[str],
    cwd: str,
    timeout_seconds: int,
    heartbeat: Callable[[], None] | None = None,
    artifact_writer: Callable[[int, str, str, int], None] | None = None,
    env: Mapping[str, str] | None = None,
    on_start: Callable[[int | None], None] | None = None,
    remaining_budget: Callable[[], float] | None = None,
    command_runner: Callable[..., tuple[str, int]] | None = None,
) -> list[MechanicalFailure]:
    """Run mechanical checker commands and classify failures via failure_detector.

    `env` is optional and defaults to `None`, which preserves the historical behavior of
    inheriting the caller's full `os.environ` (LP-1's `loop_step.py` relies on this default).
    LP-2's `loop_driver.py` passes an isolated, push-credential-stripped env (SEC-C1) since
    mechanical commands here execute Maker-authored code (e.g. `pytest -q` importing it) and
    must not run with the driver's own push-capable environment.

    Callers needing heartbeat-triggered kill-tree parity may pass `on_start` to track each
    subprocess pid (F5); omitting it preserves the previous behavior exactly.

    `remaining_budget` (Issue #219 P2-2, optional): a zero-argument callable returning the
    caller's current wall-clock budget remaining. Without it, every command reuses the same
    `timeout_seconds` cap, so N commands can overshoot the caller's overall deadline by up to N
    times `timeout_seconds`. Passing it caps each command's timeout to
    `min(timeout_seconds, remaining_budget())`, re-evaluated per command; once the budget is
    exhausted (`<= 0`), that command and every one after it is recorded as a synthetic timeout
    (exit code 124, matching a real per-command timeout so `failure_detector` classifies it the
    same) without spawning a subprocess. Omitting it preserves the previous behavior exactly.
    """
    detector = _load_failure_detector()
    failures: list[MechanicalFailure] = []
    for index, command in enumerate(commands, start=1):
        output: str | None = None
        exit_code: int | None = None
        command_timeout = timeout_seconds
        if remaining_budget is not None:
            command_timeout = max(min(timeout_seconds, remaining_budget()), 0)
        try:
            if command_timeout <= 0:
                output = "\ncommand skipped: wall-clock budget exhausted"
                exit_code = 124
            else:
                runner = command_runner or _run_mechanical_command
                output, exit_code = runner(
                    command, cwd, command_timeout, env=env, on_start=on_start
                )
            response = {"exit_code": exit_code, "stdout": output}
            result = detector.analyze("Bash", {"command": command}, response)
        finally:
            if heartbeat is not None:
                heartbeat()
            if artifact_writer is not None and output is not None and exit_code is not None:
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
    """Reacquire an existing stale lease for attach.

    SN-flock: held under the same purge-independent `held_coord_lock` as `resume` (see its
    docstring), so `attach()` (which calls this) also correctly blocks against, and is
    blocked by, a concurrent purge of this loop_id instead of racing it.
    """
    with held_coord_lock(loop_id, project_dir):
        path = lock_path(loop_id, project_dir)
        if not path.exists():
            raise LockNotFoundError(str(path))
        host = host or socket.gethostname()
        lock = _reacquire_stale_lock(path, owner_id, ttl_seconds, host)
        if lock is None:
            raise LockNotFoundError(str(path))
        return lock


def _reacquire_stale_lock(
    path: Path, owner_id: str, ttl_seconds: int, host: str
) -> LockInfo | None:
    """Validate staleness and issue a fresh lease within a single flock section (DC2).

    The previous implementation read the existing lock (`_read_lock`), checked staleness,
    then called `_replace_lock` (which re-flocks and unconditionally overwrites) and re-read
    the result via a *second*, separately-flocked `_read_lock`. Two concurrent `attach`
    callers could each pass the unguarded staleness check, then race inside `_replace_lock`'s
    flock: whichever writes last "wins," but the loser's post-write `_read_lock` call
    observes the winner's token and returns it as its own `LockInfo`, so both callers end up
    believing they hold the lease exclusively (double-attach). Reading, validating, and
    writing the new lease within one held flock section closes this window: the loser now
    observes the winner's *live* new lease inside the very flock it is about to write under,
    and raises `ForeignLeaseError` instead of silently adopting it.
    """
    fd = _open_lock_for_update(path)
    if fd is None:
        return None
    try:
        with os.fdopen(fd, "r+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            if not _fd_matches_path(f.fileno(), path):
                return None
            existing = _read_lock_stream(f)
            if existing is not None and is_lease_alive(existing):
                raise ForeignLeaseError(existing)
            new_lock = _new_lock(owner_id, ttl_seconds, host)
            _rewrite_locked_file(f, asdict(new_lock))
            return new_lock
    except OSError:
        return None


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


@contextmanager
def guarded_lease_section(loop_id: str, project_dir: str, lease_token: str) -> Iterator[None]:
    """Hold the lock-file's flock for the duration of a lease-gated write (review #3).

    `validate_lease()` followed by unguarded journal/state writes has a TOCTOU window: the
    lease can expire and be reacquired by another worker (`acquire_lock`/`reacquire_lease`,
    which take this same lock-file flock) between the check and the write, letting a stale
    worker clobber the new owner's state. Holding the flock here for validation and the
    caller's writes serializes both against any concurrent lease (re)acquisition. Raises
    `WriteRejectedError` (fail-closed) if the caller-held lease is invalid/stale when the
    flock is acquired.
    """
    path = lock_path(loop_id, project_dir)
    fd = _open_lock_for_update(path)
    if fd is None:
        raise WriteRejectedError(f"invalid lease for {loop_id}; refusing terminal write")
    with os.fdopen(fd, "r+", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            lock = _read_lock_stream(f) if _fd_matches_path(f.fileno(), path) else None
        except OSError:
            lock = None
        if lock is None or lock.lease_token != lease_token or not is_lease_alive(lock):
            raise WriteRejectedError(f"invalid lease for {loop_id}; refusing terminal write")
        yield


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
    *,
    precedent_push_check: bool = False,
) -> LoopState:
    """Create an initial pending state.

    `precedent_push_check` (Issue #196 follow-up, default `False`): gates the seed-on-creation
    `_remote_head()` query below behind the same opt-in flag `propose()`/`complete()` use (see
    `start()`'s own docstring). `start()` -- and therefore this function -- is called by both
    LP-1 (`loop_step.py`, which passes `True`) and LP-2 (`loop_driver.py`, which never opts in),
    so defaulting the query itself to off is required for LP-2 (and any other non-opted-in
    caller of `start()`) to stay byte-for-byte unaffected: no extra network-bound
    `git ls-remote` round trip on every loop creation, and no `remote_head_baseline` written to
    state/the `loop_created` journal payload.
    """
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
        # Issue #196 (LP-1 push-integrity warning): when opted in, seed the baseline from a live
        # query at loop creation, not `None`, so `_detect_precedent_push()` can already flag
        # drift during the loop's very first phase (a new-Issue branch's
        # `LP1_REMOTE_HEAD_ABSENT` sentinel is a perfectly valid starting baseline -- a
        # subsequent non-absent head at the next propose() would then correctly read as drift,
        # i.e. something pushed it before this driver did).
        remote_head_baseline=(
            _remote_head(worktree_path, branch) if precedent_push_check else None
        ),
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


def _ensure_unchanged_since(loop_id: str, project_dir: str, expected_version: int) -> None:
    """Raise if state.json's version changed since it was read (DH1).

    Used inside a `guarded_lease_section` immediately before a fenced write, in addition to
    that section's own lease validation: it protects against a write landing on top of a
    state.json that has moved on since this caller last read it, closing the TOCTOU gap
    between `propose`/`complete`/`reconcile`'s initial `load_state` and their eventual write.
    """
    fresh = load_state(loop_id, project_dir)
    if fresh.state_version != expected_version:
        raise WriteRejectedError(f"state changed since read for {loop_id}; refusing stale write")


def _is_stale_complete(state: LoopState, action_id: str, state_version: int) -> bool:
    """Return True if complete arguments do not match pending state."""
    pending = state.pending_action
    return pending is None or pending.action_id != action_id or state.state_version != state_version


def _completed_payload(
    action: str, result: dict[str, Any], remote_head_refresh: str | None = None
) -> dict[str, Any]:
    """Build completed journal payload.

    `remote_head_refresh` (Issue #196 PR review round 2, "Persist remote head for crash
    replay"): the already-precomputed remote HEAD `complete()` is about to seed as the new
    `remote_head_baseline`, or `None` when no refresh applies for this completion. Included in
    the payload only when not `None` so `_reconcile_from_payload()` can replay the exact same
    baseline a crash between this journal write and `_write_state()` would otherwise lose --
    without this, recovery had no way to restore the baseline for a legitimate push, and the next
    proposal could emit a false `push_integrity_warning` or fail to flag a later real one.
    """
    payload = {"action": action, "result": result}
    check_result = _extract_check_result_payload(result)
    if check_result is not None:
        payload["check_result"] = check_result
    if remote_head_refresh is not None:
        payload["remote_head_refresh"] = remote_head_refresh
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


def _non_blocking_open_from_last_check(state: LoopState) -> list[dict[str, Any]]:
    """Return the non-blocking (medium/low) findings still open at `exit_success` time.

    Sourced from the last completed phase check's `non_blocking_open` metadata (see
    `pr_review_wait.phase_check_from_review_findings`). Empty for phases/loop definitions that
    don't produce that metadata key (e.g. `exit_success` reached from a non-`pr_review_response`
    phase), so this is always safe to call regardless of which phase is exiting (issue #213).
    """
    last_check = state.last_check_result
    if not isinstance(last_check, dict):
        return []
    metadata = last_check.get("metadata")
    if not isinstance(metadata, dict):
        return []
    items = metadata.get("non_blocking_open")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


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


def _wait_external_review_had_required_push(
    state: LoopState, loop_id: str | None, project_dir: str | None
) -> bool:
    """Return True when a `wait_external_review` proposal for `state` requires a push.

    True exactly when `run_maker` was the last completed action (addressing PR review
    comments, about to be pushed before waiting on the next review round) -- shared by
    `_proposal_params()` (which surfaces this as the `push_required` proposal param the
    orchestrator acts on) and `apply_action_effect()`'s `WAIT_EXTERNAL_REVIEW` branch (Issue
    #196: which refreshes `remote_head_baseline` after that same push lands), so the two can
    never drift apart into disagreeing about whether this action pushed.

    `loop_id`/`project_dir` are `None` in some direct unit-test call sites that never reach a
    `wait_external_review` completion needing this; treated as "no push" (False) rather than
    raising, matching `_last_completed_action_name()`'s own fail-soft, journal-lookup contract.
    """
    if loop_id is None or project_dir is None:
        return False
    return _last_completed_action_name(loop_id, project_dir, state) == Action.RUN_MAKER.value


_DRAFT_PR_PUSH_EXEC_STEPS = frozenset({"pr_create_draft", "pr_to_draft", "pr_mark_draft"})
"""`on_failure.exec` tokens that push the branch (mirrors `LoopDriver._run_failure_exec()`'s own
handling of these same tokens in `scripts/loop_driver.py`) -- used by
`_exit_failure_requires_push()` below to decide whether an `exit_failure` completion's remote-head
baseline needs refreshing."""


def _exit_failure_requires_push(state: LoopState, project_dir: str | None) -> bool:
    """Return True when the current phase's `on_failure.exec` includes a Draft-PR push step.

    Shared by `_precompute_remote_head_refresh()` and `apply_action_effect()`'s `EXIT_FAILURE`
    branch (Issue #196 PR review round 2, "Refresh after failure-exit draft pushes") so the two
    can never drift apart into disagreeing about whether this completion pushed -- mirrors
    `_wait_external_review_had_required_push()`'s own role for `wait_external_review` above.

    `project_dir` is `None` in some direct unit-test call sites that never reach an
    `exit_failure` completion needing this; treated as "no push" (False) rather than raising.
    """
    if project_dir is None:
        return False
    phase_def = _load_phase_definition(state, project_dir)
    steps = _phase_nested(phase_def, ("on_failure", "exec"), [])
    if not isinstance(steps, list):
        return False
    return any(step in _DRAFT_PR_PUSH_EXEC_STEPS for step in steps)


def _next_action_iteration(state: LoopState, action: str) -> int:
    """Return action iteration number."""
    counters = state.guards.setdefault(state.phase, GuardCounters())
    if action == Action.RUN_MAKER.value:
        return counters.iteration + 1
    return max(counters.iteration, 1)


def _proposal_params(state: LoopState, action: str, project_dir: str) -> dict[str, Any]:
    """Build action-specific proposal params from durable state and loop definition."""
    if action == Action.STOP.value:
        return {
            "stop_reason": _normalize_stop_reason(state.stop_reason or "safety_stop"),
            "pr_number": state.pr_number,
        }
    if action == Action.EXIT_SUCCESS.value:
        return {
            "pr_number": state.pr_number,
            "non_blocking_open": _non_blocking_open_from_last_check(state),
        }

    phase_def = _load_phase_definition(state, project_dir)
    if action == Action.RUN_MAKER.value:
        configured_agent = _phase_nested(phase_def, ("maker", "agent"), None)
        return {
            "maker_agent": (
                state.maker_agent
                if state.definition_id == "issue-loop" and state.maker_agent
                else configured_agent
            ),
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
        params["push_required"] = _wait_external_review_had_required_push(
            state, state.loop_id, project_dir
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


def _persist_selected_maker(
    state: LoopState,
    result: dict[str, Any],
    project_dir: str | None,
    selected_maker_agent: str | None = None,
    allow_legacy_maker_result: bool = False,
) -> None:
    """初回 Maker の選定結果だけを allowlist 検証後に永続化する。"""
    if allow_legacy_maker_result and state.maker_agent is None and "maker" not in result:
        return
    agent = selected_maker_agent
    if agent is None:
        agent = _selected_maker_from_result(state, result, project_dir)
    if state.maker_agent is None and agent is not None:
        state.maker_agent = agent


def _selected_maker_from_result(
    state: LoopState, result: dict[str, Any], project_dir: str | None
) -> str:
    """Maker result の agent を取得し、初回 allowlist と以後の同一性を検証する。"""
    maker = result.get("maker")
    if not isinstance(maker, dict):
        raise ProtocolViolationError("maker result must include maker.agent")
    agent = maker.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        raise ProtocolViolationError("maker agent must be a non-empty string")
    if state.maker_agent is not None:
        if agent != state.maker_agent:
            raise ProtocolViolationError(
                f"maker agent mismatch: expected {state.maker_agent}, got {agent}"
            )
        return agent
    config = _load_loop_config(project_dir or state.worktree_path)
    allowed = _nested(config, ("maker", "allowed_agents"), [])
    if not isinstance(allowed, list) or agent not in allowed:
        raise ProtocolViolationError(f"maker agent is not allowed: {agent}")
    return agent


def _current_branch(worktree_path: str) -> str:
    """Return the current branch for a loop worktree."""
    return _git_stdout(["branch", "--show-current"], worktree_path)


# --- LP-1 push-integrity warning (Issue #196) --------------------------------------------
#
# LP-1's Maker runs as an in-session `Task` subagent, not the `claude -p` child process
# LP-2's `loop_driver.py` spawns -- so the tool-level guard LP-2 wires per-child-process
# (`maker_bash_guard.py`'s `--settings` injection, `loop_driver_support.get_remote_head()` /
# `classify_push_integrity()`) has nothing to attach to here (there is no separate process
# boundary between the orchestrator and the Maker Task to inject settings into). Issue #196
# observed a real Maker push slipping through this LP-1 prompt-only boundary. This is the
# "at minimum" fallback the issue calls for: a *detection*, not a *block* -- record a warning
# journal event when the branch's remote HEAD has drifted from the last baseline this
# orchestrator itself observed (a telltale sign something pushed to it without going through
# `advance_phase`), so the human driving LP-1 gets a signal without the loop being forced to a
# hard stop the way LP-2's unattended EV-80/EV-82 treatment does for the same signal.

LP1_REMOTE_HEAD_ABSENT = "<remote-branch-absent>"
"""`_remote_head()` sentinel for a *confirmed* absence: `git ls-remote` succeeded but found no
ref for the branch on `origin` (e.g. a brand-new loop's branch, never pushed yet). Kept distinct
from `None` (the query itself failed) so a first-ever push is never mistaken for a violation."""

REMOTE_LS_TIMEOUT_SECONDS = 10
"""Timeout for the network-bound `git ls-remote` call in `_remote_head()` (higher than the
local-only `GIT_TIMEOUT_SECONDS`, mirroring LP-2's `get_remote_head()` default)."""


def _remote_head(worktree_path: str, branch: str) -> str | None:
    """Return the current `origin` remote HEAD sha for `branch`, or a sentinel/`None`.

    Three distinguishable outcomes (mirrors LP-2's `loop_driver_support.get_remote_head()`
    contract):

    - a real sha string when `git ls-remote` finds `branch` on `origin`.
    - `LP1_REMOTE_HEAD_ABSENT` when the query succeeded but `branch` does not exist on `origin`.
    - `None` when the query itself could not be completed (process error, timeout, non-zero
      exit) -- unverifiable, callers must not treat this as "no drift".

    Issue #196 PR review round 2 (harden against config rewrites): a bare `git ls-remote origin
    ...` resolves `"origin"` through the shared worktree's mutable local git config, so a
    noncompliant Maker that writes a `[url "<evil>"] insteadOf = <origin-url>` (or `pushurl`)
    entry there before the next `propose()`/`complete()` call could silently redirect this
    probe to an attacker-chosen remote while `remote.origin.url` itself still looks unchanged
    (the repo-identity check reads that same, untouched key and would not catch the rewrite).
    Delegates to LP-2's already-hardened `loop_driver_support` helpers instead of the bare call:
    `find_dangerous_local_git_config()` refuses to probe at all (fails closed to this function's
    own `None`/"unverifiable" outcome, which `_detect_precedent_push()` already treats as
    "nothing to warn about this cycle" rather than a false accusation) the moment any
    `insteadOf`/`pushurl`/credential-helper/hook-style key is present, and `get_remote_head()` is
    given a freshly resolved *literal* origin URL (`resolve_origin_url()`) plus
    `hardened_git_config_args()` so the query itself cannot be hijacked via bare-name resolution
    even when no dangerous key was (yet) found. Imported locally (function scope, mirrors this
    module's own `checker_pass_criteria()`/`import loop_definition` pattern) rather than at
    module scope: `loop_driver_support` already imports this module at its own top level, so a
    module-scope import here would be circular.
    """
    lib_dir = Path(__file__).resolve().parent
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    import loop_driver_support as lds

    if lds.find_dangerous_local_git_config(worktree_path, REMOTE_LS_TIMEOUT_SECONDS) is not None:
        return None
    origin_url = lds.resolve_origin_url(worktree_path, REMOTE_LS_TIMEOUT_SECONDS)
    return lds.get_remote_head(
        worktree_path, branch, origin_url=origin_url, timeout_seconds=REMOTE_LS_TIMEOUT_SECONDS
    )


def _detect_precedent_push(state: LoopState) -> str | None:
    """Return the observed remote head when it drifted from the recorded baseline, else `None`.

    Returns `None` (nothing to warn about) when there is no baseline yet, the query failed
    (unverifiable -- fail silent, not fail warn, since this is a best-effort signal rather than
    the hard stop LP-2 uses for the equivalent unverifiable case), or the observed head matches
    the baseline exactly.
    """
    if state.remote_head_baseline is None:
        return None
    current = _remote_head(state.worktree_path, state.branch)
    if current is None or current == state.remote_head_baseline:
        return None
    return current


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


def _apply_remote_head_refresh(
    state: LoopState,
    *,
    should_refresh: bool,
    remote_head_refresh: str | None | _NotPrecomputed,
) -> None:
    """Assign `state.remote_head_baseline` from an already-observed remote HEAD (Issue #196).

    Only overwrites `state.remote_head_baseline` when the observed value is not `None` (a real
    sha or the confirmed-absent sentinel): a transient `git ls-remote` failure (`_remote_head()`
    returning `None`) must not clobber a previously known-good baseline with `None`, or a single
    flaky network blip would silently and permanently disable `_detect_precedent_push()` for the
    rest of the loop (every future comparison short-circuits on `baseline is None`) until the
    next successful refresh happens to land. Keeping the last known value lets the *next* refresh
    attempt retry instead.

    `remote_head_refresh` (PR review high-severity fix): when this is the
    `_REMOTE_HEAD_NOT_PRECOMPUTED` sentinel (the default for direct/backward-compatible callers,
    e.g. unit tests calling `apply_action_effect()`/`_apply_advance_phase()` standalone rather
    than through `complete()`), falls back to querying `_remote_head()` right here, gated by
    `should_refresh` -- the original, pre-follow-up behavior. `complete()` itself never hits that
    fallback: it precomputes the query result *before* acquiring its `guarded_lease_section` flock
    (see `complete()`) and always passes the resolved value through, so this function never
    performs network I/O while that lock is held.
    """
    if isinstance(remote_head_refresh, _NotPrecomputed):
        if not should_refresh:
            return
        remote_head_refresh = _remote_head(state.worktree_path, state.branch)
    if remote_head_refresh is not None:
        state.remote_head_baseline = remote_head_refresh


def _precompute_remote_head_refresh(
    state: LoopState,
    action: str,
    loop_id: str | None,
    project_dir: str | None,
    *,
    precedent_push_check: bool,
) -> str | None:
    """Query the remote HEAD for `complete()`'s upcoming baseline refresh, before its lock.

    Mirrors `propose()`'s own `_detect_precedent_push()` call, which likewise runs its
    network-bound `_remote_head()` query before entering `guarded_lease_section` rather than
    inside it (Issue #196 follow-up, PR review high-severity fix): `apply_action_effect()`'s
    `WAIT_EXTERNAL_REVIEW`/`ADVANCE_PHASE` branches used to call `_remote_head()` -- an up-to-
    `REMOTE_LS_TIMEOUT_SECONDS` (10s) `git ls-remote` subprocess -- from inside `complete()`'s
    held flock, blocking every other flock-holding operation for as long as that query took.

    Returns `None` when no refresh applies for this `action`/`precedent_push_check` combination,
    matching the conditions `apply_action_effect()`'s branches used to gate the query on
    internally. A plain remote-head string, `LP1_REMOTE_HEAD_ABSENT`, or `None` (query
    unverifiable) all flow straight through to `apply_action_effect(..., remote_head_refresh=...)`
    unchanged -- `complete()` never needs to re-derive whether a refresh was warranted.

    `EXIT_FAILURE` (Issue #196 PR review round 2, "Refresh after failure-exit draft pushes"): a
    phase whose `on_failure.exec` pushes a Draft PR branch (`_exit_failure_requires_push()`) is
    itself a legitimate push exactly like the `ADVANCE_PHASE`/`WAIT_EXTERNAL_REVIEW` cases above,
    and `resume()` explicitly allows resuming a `failed` loop back to `running` -- so this must be
    refreshed here too, or that push looks like drift at the *next* `propose()` call.
    """
    if not precedent_push_check:
        return None
    if action == Action.ADVANCE_PHASE.value:
        return _remote_head(state.worktree_path, state.branch)
    if action == Action.WAIT_EXTERNAL_REVIEW.value and _wait_external_review_had_required_push(
        state, loop_id, project_dir
    ):
        return _remote_head(state.worktree_path, state.branch)
    if action == Action.EXIT_FAILURE.value and _exit_failure_requires_push(state, project_dir):
        return _remote_head(state.worktree_path, state.branch)
    return None


def _apply_advance_phase(
    state: LoopState,
    result: dict[str, Any],
    *,
    precedent_push_check: bool = False,
    remote_head_refresh: str | None | _NotPrecomputed = _REMOTE_HEAD_NOT_PRECOMPUTED,
) -> None:
    """Apply a previously proposed phase advance.

    `precedent_push_check` (Issue #196, default `False`): when set, refreshes
    `remote_head_baseline` to the current remote HEAD -- `apply_action_effect()` only reaches
    this function for an `ADVANCE_PHASE` completion that already cleared the branch/
    repo-identity push guard (`_apply_safety_stop_if_needed`), so the orchestrator's own push
    for this action, if any, has already landed by the time `complete()` runs. Re-observing now
    keeps the baseline current for `_detect_precedent_push()`'s next comparison, regardless of
    whether this advance had a `next_phase` (the early-return branch below) or not. Left `False`
    by default because LP-2 (`loop_driver.py`) shares this function via `complete()` and already
    has its own, separate push-integrity mechanism -- see `propose()`'s own docstring.

    `remote_head_refresh`: see `_apply_remote_head_refresh()`'s own docstring.
    """
    _apply_remote_head_refresh(
        state, should_refresh=precedent_push_check, remote_head_refresh=remote_head_refresh
    )
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
    if decision.disposition == Action.STOP.value:
        _record_reviewer_unavailable_comments(state, phase_check)
        state.status = "stopped"
        state.stop_reason = decision.reason or "safety_stop"
        return
    state.status = "failed"
    state.stop_reason = decision.reason or "guard_failed"


def _record_reviewer_unavailable_comments(state: LoopState, phase_check: PhaseCheckResult) -> None:
    """Persist unavailable reply IDs so resume cannot stop again on the same comment."""
    if phase_check.signature != "external_reviewer_unavailable":
        return
    raw_ids = phase_check.metadata.get("reviewer_unavailable_comment_ids")
    if not isinstance(raw_ids, list):
        return
    comment_ids = {
        item
        for item in raw_ids
        if isinstance(item, str) and re.fullmatch(r"issue_comment:\d+", item)
    }
    if not comment_ids:
        return
    pr_review = copy.deepcopy(state.pr_review) if isinstance(state.pr_review, dict) else {}
    processed = pr_review.get("processed_comment_ids")
    processed_ids = {str(item) for item in processed} if isinstance(processed, list) else set()
    pr_review["processed_comment_ids"] = sorted(processed_ids | comment_ids)
    state.pr_review = pr_review


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
    lease_token: str,
) -> ReconcileOutcome:
    """Resolve pending action from completed journal payload."""
    pending = state.pending_action
    if pending is None:
        return ReconcileOutcome("none", state.state_version)
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    allow_legacy_maker_result = (
        pending.action == Action.RUN_MAKER.value
        and state.definition_id == "issue-loop"
        and state.maker_agent is None
        and "maker" not in result
    )
    # Issue #196 PR review round 2 ("Persist remote head for crash replay"): replay the same
    # `remote_head_refresh` `_completed_payload()` baked into this event (rather than falling
    # back to `apply_action_effect()`'s own live `_remote_head()` query, which would be a fresh,
    # possibly different observation) so a crash between the `completed` journal write and the
    # original `_write_state()` still restores the exact baseline that legitimate push observed.
    # Already-resolved (a plain string or `None`, never the `_NotPrecomputed` sentinel), so it
    # applies regardless of `precedent_push_check` -- see `_apply_remote_head_refresh()`.
    stored_remote_head_refresh = payload.get("remote_head_refresh")
    apply_action_effect(
        state,
        pending.action,
        result,
        project_dir,
        loop_id,
        pending.action_id,
        allow_legacy_maker_result=allow_legacy_maker_result,
        remote_head_refresh=(
            stored_remote_head_refresh if isinstance(stored_remote_head_refresh, str) else None
        ),
    )
    return _finalize_reconciled(
        loop_id, project_dir, state, source, pending.action_id, result, lease_token
    )


def _reconcile_from_artifact(
    loop_id: str,
    project_dir: str,
    state: LoopState,
    action_id: str,
    artifact: str,
    lease_token: str,
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
    return _finalize_reconciled(
        loop_id, project_dir, state, "artifact", action_id, result, lease_token
    )


def _finalize_reconciled(
    loop_id: str,
    project_dir: str,
    state: LoopState,
    source: str,
    action_id: str,
    result: dict[str, Any],
    lease_token: str,
) -> ReconcileOutcome:
    """Append reconciled journal event and write state.

    DH1: the write is guarded the same way as `propose`/`complete` (see `propose()`'s
    docstring comment) so a lease that expires and is reacquired between `reconcile()`'s
    initial validation and this write cannot let a stale worker's write land underneath a
    new owner.
    """
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
    with guarded_lease_section(loop_id, project_dir, lease_token):
        _ensure_unchanged_since(loop_id, project_dir, previous_version)
        append_journal_event(
            loop_id, project_dir, "reconciled", "step", action_id, {"resolved_by": source}
        )
        _write_state(state, project_dir)
    return ReconcileOutcome(f"resolved_from_{source}", state.state_version)


def _mark_unresolved_pending(
    loop_id: str, project_dir: str, state: LoopState, lease_token: str
) -> ReconcileOutcome:
    """Mark side-effectful unresolved pending action as infrastructure failure.

    Loads the real phase definition and project config (PR #229 review) instead of always
    evaluating against `phase_def=None`/`DEFAULT_CONFIG`: this reconcile path is reachable
    from any `attach()` on any status (`running`/`waiting_external` for a later-iteration
    orphaned action, and now `pending` for an orphaned initial `run_maker`, Issue #205) and,
    before this fix, silently ignored a project's `guards.infrastructure_failure.max_retries`
    override in `loop-harness.local.yaml`, always retrying up to the package default
    (`DEFAULT_CONFIG`'s `max_retries: 3`) regardless of a lower configured value. Mirrors
    `_apply_maker_infrastructure_failure`'s sibling `if project_dir else` pattern, but without
    the `None` guard since `project_dir` is required here (`reconcile()`'s own signature).
    """
    phase_check = PhaseCheckResult(False, [], "pending_action_unresolved_after_crash", True)
    state.last_check_result = phase_check_to_dict(phase_check)
    phase_def = _load_phase_definition(state, project_dir)
    config = _load_loop_config(project_dir)
    decision = evaluate_guards(state, phase_check, phase_def, config)
    if decision.disposition == Action.EXIT_FAILURE.value:
        state.status = "failed"
        state.stop_reason = decision.reason
    previous_version = state.state_version
    state.pending_action = None
    state.state_version += 1
    state.updated_at = now_iso()
    # DH1: see `_finalize_reconciled` for why validation and the write share one held flock.
    with guarded_lease_section(loop_id, project_dir, lease_token):
        _ensure_unchanged_since(loop_id, project_dir, previous_version)
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
    return _drop_none_remote_head_baseline(data)


def _drop_none_remote_head_baseline(data: dict[str, Any]) -> dict[str, Any]:
    """Remove `remote_head_baseline` from a serialized state dict when it is `None`.

    Bot review finding (PR #277, Codex P2): non-opted-in callers of `start()` (default
    `precedent_push_check=False`, e.g. LP-2's `loop_driver.py`) must get state.json / the
    `loop_created` journal payload byte-for-byte identical to the shape before Issue #196
    introduced this field -- i.e. no `remote_head_baseline` key at all, not a `null` value.
    `_state_from_dict()`'s `data.get("remote_head_baseline")` already treats a missing key the
    same as an explicit `None`, so dropping the key here changes nothing for opted-in callers
    and restores the pre-existing shape for everyone else.
    """
    if data.get("remote_head_baseline") is None:
        data.pop("remote_head_baseline", None)
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
        maker_agent=data.get("maker_agent") if isinstance(data.get("maker_agent"), str) else None,
        remote_head_baseline=data.get("remote_head_baseline")
        if isinstance(data.get("remote_head_baseline"), str)
        else None,
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


def _run_mechanical_command(
    command: str,
    cwd: str,
    timeout_seconds: int,
    env: Mapping[str, str] | None = None,
    on_start: Callable[[int | None], None] | None = None,
) -> tuple[str, int]:
    """Run one mechanical command, killing its whole process group on timeout (review #16).

    `env=None` (the default) inherits the caller's `os.environ`, matching
    `subprocess.run`'s own default and preserving pre-SEC-C1 behavior for LP-1 callers.

    Runs in its own process group (`start_new_session=True`) so a timeout kills every
    descendant the Maker-authored command spawned (e.g. a `pytest` run's own subprocesses),
    not just the direct `bash` child; a plain `subprocess.run(..., timeout=...)` only ever
    reaps the immediate child, leaking grandchildren on timeout.

    `on_start` optionally registers and clears the child pid for heartbeat-triggered
    kill-tree handling (F5); omitting it preserves the previous behavior exactly.
    """
    proc = subprocess.Popen(
        ["bash", "-lc", command],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=dict(env) if env is not None else None,
    )
    if on_start is not None:
        on_start(proc.pid)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc.pid)
            stdout, stderr = proc.communicate()
            return f"{stdout or ''}{stderr or ''}\ncommand timed out", 124
        return f"{stdout or ''}{stderr or ''}", proc.returncode
    finally:
        if on_start is not None:
            on_start(None)


def _kill_process_group(pid: int, term_wait_seconds: float = 10.0) -> None:
    """Escalate SIGTERM -> (wait) -> SIGKILL to an entire process group.

    Standalone copy of `loop_driver_support.kill_process_tree()`'s logic: `loop_common`
    cannot import `loop_driver_support` (the latter already imports `loop_common` as `lc`,
    so a back-import would be circular).

    `PermissionError` is treated the same as `ProcessLookupError` (group gone, stop
    polling): once the group's session leader has died from SIGTERM, a same-UID
    existence-check (`killpg(pid, 0)`) on the now-orphaned group can spuriously raise
    `PermissionError` on macOS instead of `ProcessLookupError`, even though the group's
    members are already terminating/terminated.
    """
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + term_wait_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


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
    """Replace lock.json with a fresh lease without validating the old token.

    Called by `resume`/`reacquire_lease` to reissue a lease with no live owner. Takes this
    lock file's own flock before writing - whether or not the file already exists - mirroring
    the `O_CREAT | O_EXCL`-then-fallback discipline `acquire_lock`/`_acquire_existing_lock`
    already use. Without this, an unguarded `_write_json_file` atomic-rename write here never
    touches the *existing* lock file's inode/flock at all, so a concurrent flock holder on
    the same path (`loop_status.py` purge, which now holds this same flock across its
    stale-status reload + directory delete precisely to guard against this race, see
    `_purge_if_still_safe`) could delete the loop directory out from under this write, or this
    write could resurrect a directory purge just deleted.
    """
    path = lock_path(loop_id, project_dir)
    _ensure_dir(path.parent)
    host = host or socket.gethostname()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    except FileExistsError:
        _replace_existing_lock_file(path, owner_id, ttl_seconds, host)
        return
    _write_new_lock_fd(fd, owner_id, ttl_seconds, host)


def _replace_existing_lock_file(path: Path, owner_id: str, ttl_seconds: int, host: str) -> None:
    """Flock and overwrite an existing lock file unconditionally (no staleness check)."""
    fd = _open_lock_for_update(path)
    if fd is None:
        # The file vanished between our O_EXCL failure above and this open (e.g. a
        # concurrent purge deleted the whole loop dir in between). Recreate it directly;
        # a fresh O_EXCL here cannot collide with that now-finished purge.
        _ensure_dir(path.parent)
        new_fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
        _write_new_lock_fd(new_fd, owner_id, ttl_seconds, host)
        return
    with os.fdopen(fd, "r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        new_lock = _new_lock(owner_id, ttl_seconds, host)
        _rewrite_locked_file(f, asdict(new_lock))


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
