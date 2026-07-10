#!/usr/bin/env python3
"""LP-1 loop-harness CLI: thin JSON adapter over loop_common."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_LIB_DIR = _SCRIPT_DIR.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import loop_common as lc  # noqa: E402
import loop_definition as ld  # noqa: E402
import worktree_manager as wm  # noqa: E402

EXIT_SUCCESS = 0
EXIT_GENERAL_ERROR = 1
EXIT_VALIDATION_REJECTED = 2
EXIT_LOCK_UNAVAILABLE = 3
DEFAULT_DEFINITION_ID = "issue-loop"
MECHANICAL_CHECK_TIMEOUT_SECONDS = 1800
MAX_LLM_RESULT_BYTES = 1024 * 1024
MAX_LLM_REVIEWERS = 2
REQUIRED_CHECKER_LAYERS = frozenset({"mechanical", "llm_review"})
MECHANICAL_CHECKER_LAYERS = frozenset({"mechanical"})
_ISSUE_LOOP_ID_RE = re.compile(r"^[0-9a-f]{8}-issue-([1-9][0-9]*)$")
_REVIEWER_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TERMINAL_ACTIONS = frozenset(
    {lc.Action.EXIT_SUCCESS.value, lc.Action.EXIT_FAILURE.value, lc.Action.STOP.value}
)


@dataclass(frozen=True)
class CliFailure(Exception):
    """Structured CLI failure rendered as JSON."""

    code: str
    message: str
    exit_code: int
    details: dict[str, Any] = field(default_factory=dict)


class JsonArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that lets main render parse errors as JSON."""

    def error(self, message: str) -> None:
        raise CliFailure("usage_error", message, EXIT_GENERAL_ERROR)


def find_repo_root(start: Path) -> Path | None:
    """Find the git repo root by walking up from start."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def build_parser() -> argparse.ArgumentParser:
    """Build the loop_step parser."""
    parser = JsonArgumentParser(
        prog="loop_step.py",
        description="LP-1 loop-harness CLI. All non-help results are one-line JSON.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    start = subcommands.add_parser(
        "start",
        help="initialize a loop and return the first propose-equivalent action",
        description=(
            "Initialize a loop. The response is already the first proposal; "
            "do not call propose again before completing that action."
        ),
    )
    start.add_argument("--issue", required=True, type=_positive_int)
    start.add_argument("--definition", default=DEFAULT_DEFINITION_ID)
    _add_project(start)

    attach = subcommands.add_parser("attach", help="reacquire a stale lease and propose")
    attach.add_argument("--loop-id", required=True)
    _add_project(attach)

    propose = subcommands.add_parser("propose", help="propose the next action")
    propose.add_argument("--loop-id", required=True)
    propose.add_argument("--lease-token")
    _add_project(propose)

    complete = subcommands.add_parser("complete", help="complete a proposed action")
    complete.add_argument("--loop-id", required=True)
    complete.add_argument("--action-id", required=True)
    complete.add_argument("--state-version", required=True, type=int)
    complete.add_argument("--result", required=True)
    complete.add_argument("--lease-token")
    _add_project(complete)

    reconcile = subcommands.add_parser("reconcile", help="repair orphaned pending state")
    reconcile.add_argument("--loop-id", required=True)
    reconcile.add_argument("--lease-token")
    _add_project(reconcile)

    heartbeat = subcommands.add_parser("heartbeat", help="extend the current lease")
    heartbeat.add_argument("--loop-id", required=True)
    heartbeat.add_argument("--lease-token")
    _add_project(heartbeat)

    resume = subcommands.add_parser("resume", help="resume a failed/stopped loop")
    resume.add_argument("--loop-id", required=True)
    resume.add_argument("--reset-counters", action="store_true")
    _add_project(resume)

    run_checker = subcommands.add_parser(
        "run-checker", help="run deterministic checks for a pending checker action"
    )
    run_checker.add_argument("--loop-id", required=True)
    run_checker.add_argument("--action-id", required=True)
    run_checker.add_argument("--state-version", required=True, type=int)
    run_checker.add_argument("--lease-token")
    run_checker.add_argument("--llm-result", action="append", default=[])
    _add_project(run_checker)

    return parser


def cmd_start(args: argparse.Namespace) -> dict[str, Any]:
    """Handle start."""
    project = _project_dir(args.project)
    definition = _load_definition(project, args.definition)
    loop_id = wm.compute_loop_id(project, args.issue)
    if lc.state_path(loop_id, project).exists():
        raise CliFailure("already_exists", f"loop already exists: {loop_id}", EXIT_GENERAL_ERROR)
    worktree_path = Path(wm.worktree_path_for(project, args.issue))
    worktree_existed = worktree_path.exists()
    lock = lc.acquire_lock(loop_id, project, _owner_id(), _lp1_ttl(project))
    if lock is None:
        raise lc.ForeignLeaseError(None)
    worktree: wm.WorktreeInfo | None = None
    try:
        worktree = wm.create_worktree(project, args.issue)
        result = lc.start(
            loop_id=loop_id,
            project_dir=project,
            definition_id=definition.id,
            repo_identity_hash=worktree.repo_identity_hash,
            worktree_path=worktree.path,
            branch=worktree.branch,
            owner_id=_owner_id(),
            ttl_seconds=_lp1_ttl(project),
            phase=definition.phases[0].name,
            preacquired_lock=lock,
        )
    except lc.InvalidStateError as exc:
        if "state already exists" not in str(exc):
            _rollback_created_worktree(project, args.issue, worktree_path, worktree_existed)
        lc.release_lock(loop_id, project, lock.lease_token)
        raise
    except lc.ForeignLeaseError:
        lc.release_lock(loop_id, project, lock.lease_token)
        raise
    except Exception:
        _rollback_created_worktree(project, args.issue, worktree_path, worktree_existed)
        lc.release_lock(loop_id, project, lock.lease_token)
        raise
    assert worktree is not None
    _emit_loop_start(project, args.issue, worktree)
    return _proposal_response(
        loop_id, result, "loop initialized; first action proposed", project=project
    )


def cmd_attach(args: argparse.Namespace) -> dict[str, Any]:
    """Handle attach."""
    project = _project_dir(args.project)
    result = _attach_with_token(args.loop_id, project)
    return _proposal_response(args.loop_id, result, "attached after stale lease", project=project)


def cmd_propose(args: argparse.Namespace) -> dict[str, Any]:
    """Handle propose."""
    project = _project_dir(args.project)
    lease_token = _required_lease_token(args)
    _refresh_lease_or_raise(args.loop_id, project, lease_token)
    result = lc.propose(args.loop_id, project, lease_token)
    return _proposal_response(args.loop_id, result, "next action proposed", project=project)


def cmd_complete(args: argparse.Namespace) -> dict[str, Any]:
    """Handle complete."""
    project = _project_dir(args.project)
    lease_token = _required_lease_token(args)
    loaded_result = _load_result(args.result)
    _refresh_lease_or_raise(args.loop_id, project, lease_token)
    pending_state = lc.load_state(args.loop_id, project)
    if _requires_sealed_checker(pending_state, args.action_id, args.state_version):
        _validate_sealed_checker_completion(args.loop_id, project, args.action_id, loaded_result)
    else:
        _save_checker_artifact(
            args.loop_id, project, args.action_id, args.state_version, loaded_result
        )
    result = lc.complete(
        args.loop_id,
        project,
        args.action_id,
        args.state_version,
        loaded_result,
        lease_token,
    )
    if not result.idempotent_replay:
        _emit_loop_iteration(project, pending_state, loaded_result)
    return {
        "ok": result.ok,
        "loop_id": args.loop_id,
        "state_version": result.state_version,
        "next": result.next_hint,
        "idempotent_replay": result.idempotent_replay,
    }


def cmd_reconcile(args: argparse.Namespace) -> dict[str, Any]:
    """Handle reconcile."""
    project = _project_dir(args.project)
    lease_token = _required_lease_token(args)
    _refresh_lease_or_raise(args.loop_id, project, lease_token)
    pending_action_id = _pending_action_id(args.loop_id, project)
    result = lc.reconcile(args.loop_id, project, lease_token)
    reconciled = result.action_taken in {
        "resolved_from_journal",
        "resolved_from_artifact",
        "marked_infrastructure_failure",
    }
    return {
        "loop_id": args.loop_id,
        "reconciled": reconciled,
        "resolved_action_id": pending_action_id if reconciled else None,
        "resolution": _reconcile_resolution(result.action_taken),
        "state_version": result.state_version,
    }


def cmd_heartbeat(args: argparse.Namespace) -> dict[str, Any]:
    """Handle heartbeat."""
    project = _project_dir(args.project)
    lease_token = _required_lease_token(args)
    if not lc.heartbeat(args.loop_id, project, lease_token):
        raise CliFailure(
            "lease_mismatch", "invalid or expired lease token", EXIT_VALIDATION_REJECTED
        )
    lock = _read_lock_payload(args.loop_id, project)
    return {
        "loop_id": args.loop_id,
        "heartbeat_at": str(lock.get("heartbeat_at") or lc.now_iso()),
        "ttl": _lp1_ttl(project),
    }


def cmd_resume(args: argparse.Namespace) -> dict[str, Any]:
    """Handle resume."""
    if not args.reset_counters:
        raise CliFailure(
            "reset_counters_required",
            "resume requires --reset-counters",
            EXIT_GENERAL_ERROR,
        )
    project = _project_dir(args.project)
    resumed = lc.resume(args.loop_id, project, True, _owner_id(), _lp1_ttl(project))
    try:
        result = lc.propose(args.loop_id, project, resumed.lease_token)
    except Exception as exc:
        raise _failure_with_lease(
            exc, "failed to propose after resume", resumed.lease_token
        ) from exc
    result_with_lease = lc.ProposeResult(
        action=result.action,
        action_id=result.action_id,
        state_version=result.state_version,
        expected_phase=result.expected_phase,
        phase=result.phase,
        iteration=result.iteration,
        context={**result.context, "lease_token": resumed.lease_token},
    )
    return _proposal_response(
        args.loop_id, result_with_lease, "resumed; first action proposed", project=project
    )


def cmd_run_checker(args: argparse.Namespace) -> dict[str, Any]:
    """Run the checker layers without completing the pending action."""
    project = _project_dir(args.project)
    lease_token = _required_lease_token(args)
    _refresh_lease_or_raise(args.loop_id, project, lease_token)
    state = lc.load_state(args.loop_id, project)
    _validate_pending_checker(state, args.action_id, args.state_version)
    phase = ld.phase_by_name(_load_definition(project, state.definition_id), state.phase)
    commands = _mechanical_commands(phase.checker)
    has_llm_review = isinstance(phase.checker.get("llm_review"), dict)
    if has_llm_review:
        pass_criteria = lc.checker_pass_criteria(state, project)
        llm_results = _load_bound_llm_results(args.llm_result)
    else:
        if args.llm_result:
            _raise_invalid_llm_result("llm results are not allowed for a mechanical-only checker")
        pass_criteria = {}
        llm_results = []

    def heartbeat_and_validate() -> None:
        _refresh_checker_fence(
            args.loop_id,
            project,
            lease_token,
            args.action_id,
            args.state_version,
        )

    mechanical_paths: list[str] = []

    def save_mechanical_log(index: int, _command: str, output: str, _exit_code: int) -> None:
        mechanical_paths.append(
            lc.save_artifact(
                args.loop_id,
                project,
                args.action_id,
                f"mechanical_{index}.log",
                output,
            )
        )

    failures = lc.run_mechanical_checks(
        commands,
        state.worktree_path,
        MECHANICAL_CHECK_TIMEOUT_SECONDS,
        heartbeat=heartbeat_and_validate,
        artifact_writer=save_mechanical_log,
    )
    heartbeat_and_validate()
    normalized_mechanical_path = _save_mechanical_artifact(
        args.loop_id, project, args.action_id, failures
    )
    mechanical_path = ",".join(mechanical_paths) or normalized_mechanical_path
    mechanical = lc.CheckResult(
        passed=not failures,
        layer="mechanical",
        signature=lc.compute_implementation_signature(failures),
        findings=[],
        raw_artifact_path=mechanical_path,
        infrastructure_failure=any(
            failure.failure_type == "infrastructure_failure" for failure in failures
        ),
    )
    results = [mechanical]
    required_layers = MECHANICAL_CHECKER_LAYERS
    metadata: dict[str, Any] = {}
    if has_llm_review:
        llm_review = _combine_llm_results(
            _save_llm_review_artifacts(
                args.loop_id,
                project,
                args.action_id,
                llm_results,
                pass_criteria,
            ),
            pass_criteria,
        )
        results.append(llm_review)
        required_layers = REQUIRED_CHECKER_LAYERS
        metadata["reviewers"] = _bound_reviewer_names(args.llm_result)
    combined = lc.combine_check_results(results, pass_criteria, required_layers)
    sealed = lc.PhaseCheckResult(
        combined.passed,
        combined.results,
        combined.signature,
        combined.infrastructure_failure,
        metadata={**combined.metadata, **metadata},
    )
    payload = lc.redact_payload(lc.phase_check_to_dict(sealed))
    heartbeat_and_validate()
    lc.save_artifact(
        args.loop_id,
        project,
        args.action_id,
        "check_result.json",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    try:
        args = build_parser().parse_args(argv)
        response = _dispatch(args)
        _write_json(response)
        return EXIT_SUCCESS
    except CliFailure as exc:
        _write_error(exc.code, exc.message, exc.details)
        return exc.exit_code
    except lc.StaleActionError as exc:
        _write_error("stale_action", _message(exc, "stale action"))
        return EXIT_VALIDATION_REJECTED
    except lc.WriteRejectedError as exc:
        _write_error("lease_mismatch", _message(exc, "invalid lease token"))
        return EXIT_VALIDATION_REJECTED
    except lc.ProtocolViolationError as exc:
        _write_error("protocol_violation", _message(exc, "protocol violation"))
        return EXIT_VALIDATION_REJECTED
    except lc.IntegrityError as exc:
        _write_error("integrity_error", _message(exc, "integrity error"))
        return EXIT_GENERAL_ERROR
    except (lc.ForeignLeaseError, lc.LockNotFoundError) as exc:
        _write_error("lock_unavailable", _message(exc, "lock unavailable"))
        return EXIT_LOCK_UNAVAILABLE
    except lc.InvalidStateError as exc:
        code = "already_exists" if "already exists" in str(exc) else "invalid_state"
        _write_error(code, _message(exc, "invalid state"))
        return EXIT_GENERAL_ERROR
    except (lc.RootResolutionError, wm.WorktreeError, ld.DefinitionValidationError) as exc:
        _write_error(_error_code_for(exc), _message(exc, "operation failed"))
        return EXIT_GENERAL_ERROR
    except ValueError as exc:
        _write_error("validation_error", _message(exc, "validation error"))
        return EXIT_GENERAL_ERROR
    except Exception as exc:  # noqa: BLE001 - CLI boundary must never traceback-only crash.
        _write_error("internal_error", _message(exc, "unexpected error"))
        return EXIT_GENERAL_ERROR


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    """Dispatch parsed args to a command handler."""
    handlers = {
        "start": cmd_start,
        "attach": cmd_attach,
        "propose": cmd_propose,
        "complete": cmd_complete,
        "reconcile": cmd_reconcile,
        "heartbeat": cmd_heartbeat,
        "resume": cmd_resume,
        "run-checker": cmd_run_checker,
    }
    return handlers[args.command](args)


def _add_project(parser: argparse.ArgumentParser) -> None:
    """Add the common --project option."""
    parser.add_argument("--project", help="project root; defaults to the nearest git root")


def _positive_int(value: str) -> int:
    """Parse a positive integer."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _project_dir(value: str | None) -> str:
    """Resolve project dir from --project or cwd."""
    if value:
        return str(Path(value).resolve())
    root = find_repo_root(Path.cwd())
    if root is None:
        raise CliFailure("repo_not_found", "could not find git repository root", EXIT_GENERAL_ERROR)
    return str(root)


def _load_definition(project: str, definition_id: str) -> ld.LoopDefinition:
    """Load one loop definition by id."""
    definitions = ld.load_all_definitions(project)
    definition = definitions.get(definition_id)
    if definition is None:
        raise CliFailure(
            "definition_not_found",
            f"loop definition not found: {definition_id}",
            EXIT_GENERAL_ERROR,
        )
    return definition


def _lp1_ttl(project: str) -> int:
    """Return configured LP-1 lease TTL."""
    config = ld.load_config(project)
    value = _nested(config, ("lock", "ttl_seconds", "lp1"), 3600)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CliFailure(
            "invalid_config", "lock.ttl_seconds.lp1 must be an integer", EXIT_GENERAL_ERROR
        ) from exc


def _nested(source: dict[str, Any], path: tuple[str, ...], default: Any) -> Any:
    """Read a nested mapping value."""
    current: Any = source
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _owner_id() -> str:
    """Return a human-readable lease owner id."""
    return f"loop_step:{socket.gethostname()}:{os.getpid()}"


def _required_lease_token(args: argparse.Namespace) -> str:
    """Return --lease-token or raise the validation exit."""
    token = getattr(args, "lease_token", None)
    if not token:
        raise CliFailure(
            "lease_token_required",
            f"{args.command} requires --lease-token",
            EXIT_VALIDATION_REJECTED,
        )
    return str(token)


def _validate_pending_checker(state: lc.LoopState, action_id: str, state_version: int) -> None:
    """Validate checker fencing inputs before launching any subprocess."""
    pending = state.pending_action
    if pending is None or pending.action_id != action_id or state.state_version != state_version:
        raise lc.StaleActionError(f"stale action: {action_id}")
    if pending.action != lc.Action.RUN_CHECKER.value or pending.phase != state.phase:
        raise lc.ProtocolViolationError("pending action is not the current phase checker")


def _refresh_checker_fence(
    loop_id: str,
    project: str,
    lease_token: str,
    action_id: str,
    state_version: int,
) -> None:
    """Refresh the lease and revalidate the pending checker generation."""
    _refresh_lease_or_raise(loop_id, project, lease_token)
    _validate_pending_checker(lc.load_state(loop_id, project), action_id, state_version)


def _mechanical_commands(checker: dict[str, Any]) -> list[str]:
    """Return validated mechanical commands from the durable loop definition."""
    commands = _nested(checker, ("mechanical", "commands"), None)
    if (
        not isinstance(commands, list)
        or not commands
        or not all(isinstance(command, str) and command for command in commands)
    ):
        raise ld.DefinitionValidationError("checker.mechanical.commands must be strings")
    return list(commands)


def _save_mechanical_artifact(
    loop_id: str,
    project: str,
    action_id: str,
    failures: list[lc.MechanicalFailure],
) -> str:
    """Persist normalized mechanical failures and return the artifact path."""
    payload = {
        "failures": [
            {
                "command": failure.command,
                "failure_type": failure.failure_type,
                "error_type": failure.error_type,
                "output": failure.output,
            }
            for failure in failures
        ]
    }
    return lc.save_artifact(
        loop_id,
        project,
        action_id,
        "mechanical.json",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


def _combine_llm_results(
    loaded: list[tuple[str, lc.CheckResult]], pass_criteria: dict[str, int]
) -> lc.CheckResult:
    """Aggregate reviewer-bound LLM review artifacts."""
    findings = [finding for _, result in loaded for finding in result.findings]
    infrastructure_failure = any(result.infrastructure_failure for _, result in loaded)
    findings_pass = all(
        sum(finding.severity == severity for finding in findings) <= limit
        for severity, limit in pass_criteria.items()
    )
    return lc.CheckResult(
        passed=findings_pass and not infrastructure_failure,
        layer="llm_review",
        signature=lc.compute_llm_review_signature(findings),
        findings=findings,
        raw_artifact_path=",".join(result.raw_artifact_path for _, result in loaded),
        infrastructure_failure=infrastructure_failure,
    )


def _save_llm_review_artifacts(
    loop_id: str,
    project: str,
    action_id: str,
    loaded: list[tuple[str, lc.CheckResult]],
    pass_criteria: dict[str, int],
) -> list[tuple[str, lc.CheckResult]]:
    """Copy normalized reviewer results into the action artifact directory."""
    persisted: list[tuple[str, lc.CheckResult]] = []
    for reviewer, result in loaded:
        name = f"llm_review_{reviewer}.json"
        relative_path = str(Path("artifacts") / action_id / name)
        passed = (
            all(
                sum(finding.severity == severity for finding in result.findings) <= limit
                for severity, limit in pass_criteria.items()
            )
            and not result.infrastructure_failure
        )
        copied = lc.CheckResult(
            passed=passed,
            layer=result.layer,
            signature=lc.compute_llm_review_signature(result.findings),
            findings=result.findings,
            raw_artifact_path=relative_path,
            infrastructure_failure=result.infrastructure_failure,
        )
        lc.save_artifact(
            loop_id,
            project,
            action_id,
            name,
            json.dumps(
                lc.check_result_to_dict(copied),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        persisted.append((reviewer, copied))
    return persisted


def _load_bound_llm_results(values: list[str]) -> list[tuple[str, lc.CheckResult]]:
    """Validate reviewer bindings and load each strict result file."""
    if not values or len(values) > MAX_LLM_REVIEWERS:
        _raise_invalid_llm_result(f"one to {MAX_LLM_REVIEWERS} reviewer results are required")
    bindings: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for value in values:
        reviewer, separator, file_reference = value.partition("=")
        if (
            not separator
            or not _REVIEWER_RE.fullmatch(reviewer)
            or not file_reference.startswith("@")
            or not file_reference[1:]
        ):
            _raise_invalid_llm_result("llm result must use <reviewer>=@<file>")
        if reviewer in seen:
            _raise_invalid_llm_result(f"duplicate reviewer: {reviewer}")
        seen.add(reviewer)
        bindings.append((reviewer, Path(file_reference[1:])))
    if "code-reviewer" not in seen:
        _raise_invalid_llm_result("code-reviewer result is required")
    return [(reviewer, _load_llm_result_file(reviewer, path)) for reviewer, path in bindings]


def _bound_reviewer_names(values: list[str]) -> list[str]:
    """Return reviewer names after reviewer bindings have been validated."""
    return [value.partition("=")[0] for value in values]


def _load_llm_result_file(reviewer: str, path: Path) -> lc.CheckResult:
    """Load one 0600, regular, non-symlink reviewer result file."""
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise CliFailure(
            "invalid_llm_result",
            f"review result file is unavailable: {path}",
            EXIT_VALIDATION_REJECTED,
        ) from exc
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        _raise_invalid_llm_result("review result must be a regular non-symlink file")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except OSError as exc:
        raise CliFailure(
            "invalid_llm_result",
            f"review result file is unavailable: {path}",
            EXIT_VALIDATION_REJECTED,
        ) from exc
    try:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            _raise_invalid_llm_result("review result must be a regular non-symlink file")
        if stat.S_IMODE(opened_stat.st_mode) != lc.FILE_MODE:
            _raise_invalid_llm_result("review result file mode must be 0600")
        if opened_stat.st_size > MAX_LLM_RESULT_BYTES:
            _raise_invalid_llm_result("review result file exceeds size limit")
        with os.fdopen(fd, encoding="utf-8") as file:
            fd = -1
            data = json.load(file)
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise CliFailure(
            "invalid_llm_result",
            f"review result is not valid JSON: {path}",
            EXIT_VALIDATION_REJECTED,
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not _is_valid_llm_result(data):
        _raise_invalid_llm_result(f"review result has invalid schema: {path}")
    try:
        result = lc.check_result_from_dict(data)
    except (TypeError, ValueError) as exc:
        raise CliFailure(
            "invalid_llm_result",
            f"review result has invalid schema: {path}",
            EXIT_VALIDATION_REJECTED,
        ) from exc
    bound_findings = [
        lc.Finding(finding.severity, finding.summary, reviewer, finding.path, finding.line)
        for finding in result.findings
    ]
    findings = [
        lc.Finding(
            finding.severity,
            lc.redact(finding.summary),
            lc.redact(finding.source),
            lc.redact(finding.path) if finding.path is not None else None,
            finding.line,
        )
        for finding in bound_findings
    ]
    return lc.CheckResult(
        passed=result.passed,
        layer="llm_review",
        signature=result.signature,
        findings=findings,
        raw_artifact_path=result.raw_artifact_path,
        infrastructure_failure=result.infrastructure_failure,
    )


def _raise_invalid_llm_result(message: str) -> None:
    """Raise the stable reviewer result validation error."""
    raise CliFailure("invalid_llm_result", message, EXIT_VALIDATION_REJECTED)


def _is_valid_llm_result(data: Any) -> bool:
    """Return whether data has the strict serialized LLM CheckResult shape."""
    if (
        not isinstance(data, dict)
        or frozenset(data) != lc.CHECK_RESULT_KEYS
        or data.get("layer") != "llm_review"
    ):
        return False
    if not isinstance(data.get("passed"), bool):
        return False
    if not isinstance(data.get("infrastructure_failure"), bool):
        return False
    if not isinstance(data.get("raw_artifact_path"), str):
        return False
    if data.get("signature") is not None and not isinstance(data.get("signature"), str):
        return False
    findings = data.get("findings")
    return isinstance(findings, list) and all(_is_valid_finding(item) for item in findings)


def _is_valid_finding(data: Any) -> bool:
    """Return whether data can be deserialized as a Finding."""
    if not isinstance(data, dict) or frozenset(data) != lc.FINDING_KEYS:
        return False
    if data.get("severity") not in {"critical", "high", "medium", "low"}:
        return False
    if not isinstance(data.get("summary"), str) or not isinstance(data.get("source"), str):
        return False
    if data.get("path") is not None and not isinstance(data.get("path"), str):
        return False
    line = data.get("line")
    return line is None or (isinstance(line, int) and not isinstance(line, bool))


def _refresh_lease_or_raise(loop_id: str, project: str, lease_token: str) -> None:
    """Refresh the active LP-1 lease at the start of a mutating step call."""
    if not lc.heartbeat(loop_id, project, lease_token):
        raise CliFailure(
            "lease_mismatch", "invalid or expired lease token", EXIT_VALIDATION_REJECTED
        )


def _failure_with_lease(exc: BaseException, fallback: str, lease_token: str) -> CliFailure:
    """Return a CliFailure that preserves normal classification and exposes the new lease."""
    return CliFailure(
        _error_code_for(exc),
        _message(exc, fallback),
        _exit_code_for(exc),
        {"lease_token": lease_token},
    )


def _exit_code_for(exc: BaseException) -> int:
    """Return the CLI exit code that main would use for a known exception."""
    if isinstance(
        exc,
        (
            lc.StaleActionError,
            lc.WriteRejectedError,
            lc.ProtocolViolationError,
        ),
    ):
        return EXIT_VALIDATION_REJECTED
    if isinstance(exc, (lc.ForeignLeaseError, lc.LockNotFoundError)):
        return EXIT_LOCK_UNAVAILABLE
    return EXIT_GENERAL_ERROR


def _attach_with_token(loop_id: str, project: str) -> lc.ProposeResult:
    """Attach and preserve the reclaimed lease token even when proposal creation fails."""
    state = lc.load_state(loop_id, project)
    if state.status not in {"running", "waiting_external"}:
        raise lc.InvalidStateError(f"cannot attach status={state.status}")
    lock = lc.reacquire_lease(loop_id, project, _owner_id(), _lp1_ttl(project))
    try:
        result = lc.propose(loop_id, project, lock.lease_token, recover_orphans=True)
    except Exception as exc:
        raise _failure_with_lease(exc, "failed to propose after attach", lock.lease_token) from exc
    return lc.ProposeResult(
        action=result.action,
        action_id=result.action_id,
        state_version=result.state_version,
        expected_phase=result.expected_phase,
        phase=result.phase,
        iteration=result.iteration,
        context={**result.context, "lease_token": lock.lease_token},
    )


def _load_result(value: str) -> dict[str, Any]:
    """Load a complete result from JSON text or @file."""
    raw = _read_result_text(value)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliFailure(
            "invalid_result", "result must be a JSON object", EXIT_GENERAL_ERROR
        ) from exc
    if not isinstance(data, dict):
        raise CliFailure("invalid_result", "result must be a JSON object", EXIT_GENERAL_ERROR)
    return data


def _read_result_text(value: str) -> str:
    """Read raw result JSON."""
    if not value.startswith("@"):
        return value
    path = Path(value[1:])
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliFailure(
            "result_read_error", f"failed to read result file: {path}", EXIT_GENERAL_ERROR
        ) from exc


def _proposal_response(
    loop_id: str, result: lc.ProposeResult, reason: str, *, project: str | None = None
) -> dict[str, Any]:
    """Serialize ProposeResult to the stable CLI response shape."""
    action = lc.Action(result.action).value
    context = dict(result.context)
    lease_token = context.pop("lease_token", None)
    params = context.pop("params", {})
    if not isinstance(params, dict):
        params = {}
    if project is not None:
        state = lc.load_state(loop_id, project)
        params = {**params, **_common_proposal_params(loop_id, state)}
        if action == lc.Action.RUN_MAKER.value:
            previous_check = _previous_check_summary(state)
            if previous_check:
                params["previous_check"] = previous_check
    response_reason = str(context.pop("reason", reason) if "reason" in context else reason)
    response: dict[str, Any] = {
        "loop_id": loop_id,
        "action": action,
        "action_id": result.action_id,
        "state_version": result.state_version,
        "phase": result.phase,
        "iteration": result.iteration,
        "params": params,
        "reason": response_reason,
    }
    if lease_token is not None:
        response["lease_token"] = lease_token
    if project is not None and action in TERMINAL_ACTIONS:
        _emit_loop_stop(project, loop_id, action, params)
    return response


def _common_proposal_params(loop_id: str, state: lc.LoopState) -> dict[str, Any]:
    """Return state-derived execution context shared by every action."""
    return {
        "issue_number": _issue_number_from_loop_id(loop_id),
        "worktree_path": state.worktree_path,
        "branch": state.branch,
        "repo_identity_verified": lc.is_repo_identity_verified(state),
    }


def _issue_number_from_loop_id(loop_id: str) -> int | None:
    """Parse a positive issue number from the canonical issue-loop id."""
    match = _ISSUE_LOOP_ID_RE.fullmatch(loop_id)
    return int(match.group(1)) if match is not None else None


def _previous_check_summary(state: lc.LoopState) -> dict[str, Any]:
    """Return only Maker-safe mechanical and must-fix review context."""
    previous = state.last_check_result
    if not isinstance(previous, dict):
        return {}
    mechanical: dict[str, Any] | None = None
    findings: list[dict[str, Any]] = []
    results = previous.get("results")
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("layer") == "mechanical":
            mechanical = {
                "passed": bool(item.get("passed")),
                "signature": item.get("signature"),
                "infrastructure_failure": bool(item.get("infrastructure_failure")),
                "raw_artifact_path": item.get("raw_artifact_path"),
            }
        item_findings = item.get("findings")
        for finding in item_findings if isinstance(item_findings, list) else []:
            if not isinstance(finding, dict) or finding.get("severity") not in {
                "critical",
                "high",
            }:
                continue
            findings.append(
                {
                    "severity": finding.get("severity"),
                    "summary": finding.get("summary"),
                    "source": finding.get("source"),
                    "path": finding.get("path"),
                    "line": finding.get("line"),
                }
            )
    summary: dict[str, Any] = {"critical_high": findings}
    if mechanical is not None:
        summary["mechanical"] = mechanical
    return lc.redact_payload(summary)


def _pending_action(loop_id: str, project: str) -> lc.PendingAction | None:
    """Return the current pending action through loop_common."""
    return lc.load_state(loop_id, project).pending_action


def _pending_action_id(loop_id: str, project: str) -> str | None:
    """Return the current pending action id through loop_common."""
    pending = _pending_action(loop_id, project)
    if pending is None:
        return None
    return pending.action_id


def _save_checker_artifact(
    loop_id: str,
    project: str,
    action_id: str,
    state_version: int,
    result: dict[str, Any],
) -> None:
    """Persist checker output before state/journal completion for artifact recovery."""
    state = lc.load_state(loop_id, project)
    pending = state.pending_action
    check_result = _checker_artifact_payload(result)
    if (
        pending is None
        or pending.action != lc.Action.RUN_CHECKER.value
        or pending.action_id != action_id
        or state.state_version != state_version
        or check_result is None
    ):
        return
    lc.save_artifact(
        loop_id,
        project,
        action_id,
        "check_result.json",
        json.dumps(check_result, ensure_ascii=False, separators=(",", ":")),
    )


def _requires_sealed_checker(state: lc.LoopState, action_id: str, state_version: int) -> bool:
    """Return whether this completion is the sealed implementation checker path."""
    pending = state.pending_action
    return bool(
        state.definition_id == DEFAULT_DEFINITION_ID
        and state.phase == "implementation"
        and state.state_version == state_version
        and pending is not None
        and pending.action_id == action_id
        and pending.action == lc.Action.RUN_CHECKER.value
        and pending.phase == "implementation"
    )


def _validate_sealed_checker_completion(
    loop_id: str,
    project: str,
    action_id: str,
    caller_result: dict[str, Any],
) -> None:
    """Require a run-checker artifact that canonically matches the caller payload."""
    artifact = lc.load_artifact(loop_id, project, action_id, "check_result.json")
    if artifact is None:
        raise lc.ProtocolViolationError("sealed checker artifact is required")
    try:
        sealed = json.loads(artifact)
    except json.JSONDecodeError as exc:
        raise lc.ProtocolViolationError("sealed checker artifact is invalid") from exc
    caller = _checker_artifact_payload(caller_result)
    if caller is None:
        raise lc.ProtocolViolationError("sealed checker result is invalid")
    state = lc.load_state(loop_id, project)
    lc.validate_implementation_checker_result(state, sealed, project)
    if _canonical_json(sealed) != _canonical_json(caller):
        raise lc.ProtocolViolationError("caller checker result does not match sealed artifact")


def _canonical_json(value: Any) -> str:
    """Serialize a JSON-like value for canonical equality checks."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checker_artifact_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    """Return wrapper or raw PhaseCheckResult payload for checker artifact recovery."""
    check_result = result.get("check_result")
    if isinstance(check_result, dict):
        return check_result
    if _looks_like_phase_check_result(result):
        return result
    return None


def _looks_like_phase_check_result(result: dict[str, Any]) -> bool:
    """Return True for the raw PhaseCheckResult shape accepted by loop_common."""
    return (
        isinstance(result.get("passed"), bool)
        and isinstance(result.get("signature"), str)
        and isinstance(result.get("infrastructure_failure"), bool)
        and isinstance(result.get("results"), list)
    )


def _emit_loop_start(project: str, issue: int, worktree: wm.WorktreeInfo) -> None:
    """Emit loop_start after state/worktree initialization."""
    loop_id = wm.compute_loop_id(project, issue)
    state = lc.load_state(loop_id, project)
    lc.emit_loop_audit_event(
        "loop_start",
        project,
        {
            "loop_id": loop_id,
            "definition_id": state.definition_id,
            "issue_number": issue,
            "worktree_path": worktree.path,
            "branch": worktree.branch,
            "trigger": "lp1",
        },
    )


def _emit_loop_iteration(project: str, pending_state: lc.LoopState, result: dict[str, Any]) -> None:
    """Emit loop_iteration after a non-idempotent complete updates state."""
    pending = pending_state.pending_action
    if pending is None:
        return
    state = lc.load_state(pending_state.loop_id, project)
    guard = state.guards.get(pending.phase)
    payload = lc.build_audit_payload(
        "loop_iteration",
        state,
        action_id=pending.action_id,
        maker=_maker_audit_payload(pending.action, result),
        checker=_checker_audit_payload(pending.action, result),
    )
    payload.update(
        {
            "guard_snapshot": _guard_snapshot(guard),
            "result": _iteration_result(state, pending.action),
        }
    )
    lc.emit_loop_audit_event("loop_iteration", project, payload)


def _emit_loop_stop(project: str, loop_id: str, action: str, params: dict[str, Any]) -> None:
    """Emit loop_stop when propose returns a terminal action."""
    state = lc.load_state(loop_id, project)
    final_status = "stopped" if action == lc.Action.STOP.value else action
    lc.emit_loop_audit_event(
        "loop_stop",
        project,
        {
            "loop_id": loop_id,
            "phase": state.phase,
            "final_status": final_status,
            "stop_reason": params.get("stop_reason") or state.stop_reason,
            "iterations_total": _iterations_total(project, loop_id, state),
            "pr_number": state.pr_number,
        },
    )


def _maker_audit_payload(action: str, result: dict[str, Any]) -> dict[str, Any]:
    """Return best-effort maker audit details from a complete result."""
    if action != lc.Action.RUN_MAKER.value:
        return {}
    maker = result.get("maker")
    return dict(maker) if isinstance(maker, dict) else {}


def _checker_audit_payload(action: str, result: dict[str, Any]) -> dict[str, Any]:
    """Return best-effort checker audit details from a checker result."""
    if action != lc.Action.RUN_CHECKER.value:
        return {}
    check_result = (
        result.get("check_result") if isinstance(result.get("check_result"), dict) else result
    )
    results = check_result.get("results") if isinstance(check_result, dict) else []
    if not isinstance(results, list):
        return {}
    payload: dict[str, Any] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        layer = item.get("layer")
        findings = item.get("findings") if isinstance(item.get("findings"), list) else []
        if layer == "mechanical":
            payload["mechanical"] = {
                "passed": bool(item.get("passed")),
                "signature": item.get("signature"),
                "infrastructure_failure": bool(item.get("infrastructure_failure")),
            }
        if layer == "llm_review":
            payload["llm_review"] = {
                "passed": bool(item.get("passed")),
                "critical": _finding_count(findings, "critical"),
                "high": _finding_count(findings, "high"),
            }
    return payload


def _finding_count(findings: list[Any], severity: str) -> int:
    """Count checker findings by severity."""
    return sum(
        1 for item in findings if isinstance(item, dict) and item.get("severity") == severity
    )


def _guard_snapshot(counter: lc.GuardCounters | None) -> dict[str, Any]:
    """Return audit-safe guard counter state."""
    if counter is None:
        return {}
    return {
        "iteration": counter.iteration,
        "no_progress_count": counter.no_progress_streak,
        "infrastructure_failure_count": counter.infrastructure_failure_count,
    }


def _iteration_result(state: lc.LoopState, action: str | None = None) -> str:
    """Map state after complete to loop_iteration.result."""
    if action == lc.Action.ADVANCE_PHASE.value:
        return lc.Action.ADVANCE_PHASE.value
    if state.status == "failed":
        return lc.Action.EXIT_FAILURE.value
    if state.status == "passed":
        return lc.Action.EXIT_SUCCESS.value
    if state.status == "stopped":
        return lc.Action.STOP.value
    if isinstance(state.last_check_result, dict) and state.last_check_result.get("next_phase"):
        return lc.Action.ADVANCE_PHASE.value
    return "continue"


def _iterations_total(project: str, loop_id: str, state: lc.LoopState) -> int:
    """Count completed checker attempts, falling back to guard counters."""
    guard_total = sum(counter.iteration for counter in state.guards.values())
    checker_total = _completed_action_count(project, loop_id, lc.Action.RUN_CHECKER.value)
    return max(guard_total, checker_total)


def _completed_action_count(project: str, loop_id: str, action: str) -> int:
    """Return completed journal event count for an action."""
    path = lc.journal_path(loop_id, project)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    total = 0
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event.get("event") == "completed" and payload.get("action") == action:
            total += 1
    return total


def _reconcile_resolution(action_taken: str) -> str:
    """Map core reconcile action names to CLI resolution values."""
    return {
        "none": "none",
        "resolved_from_journal": "journal_restored",
        "resolved_from_artifact": "artifact_restored",
        "marked_infrastructure_failure": "marked_infrastructure_failure",
        "rerun_required": "none",
        "unresolved_pending": "none",
    }.get(action_taken, "none")


def _rollback_created_worktree(
    project: str, issue: int, worktree_path: Path, worktree_existed: bool
) -> None:
    """Remove a worktree created by start when state initialization fails."""
    if worktree_existed or not worktree_path.exists():
        return
    try:
        wm.remove_worktree(project, issue, force=True)
    except wm.WorktreeError:
        pass


def _read_lock_payload(loop_id: str, project: str) -> dict[str, Any]:
    """Read lock.json for heartbeat response fields."""
    try:
        data = json.loads(lc.lock_path(loop_id, project).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(payload: dict[str, Any]) -> None:
    """Write exactly one JSON object line to stdout."""
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _write_error(code: str, message: str, details: dict[str, Any] | None = None) -> None:
    """Write machine-readable stdout and human-readable stderr diagnostics."""
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        payload.update(details)
    _write_json(payload)
    print(f"loop_step: {code}: {message}", file=sys.stderr)


def _message(exc: BaseException, fallback: str) -> str:
    """Return a safe diagnostic message."""
    return str(exc) or fallback


def _error_code_for(exc: BaseException) -> str:
    """Return a stable error code for known general errors."""
    if isinstance(exc, lc.StaleActionError):
        return "stale_action"
    if isinstance(exc, lc.WriteRejectedError):
        return "lease_mismatch"
    if isinstance(exc, lc.ProtocolViolationError):
        return "protocol_violation"
    if isinstance(exc, (lc.ForeignLeaseError, lc.LockNotFoundError)):
        return "lock_unavailable"
    if isinstance(exc, lc.IntegrityError):
        return "integrity_error"
    if isinstance(exc, lc.RootResolutionError):
        return "root_resolution_error"
    if isinstance(exc, wm.WorktreeError):
        return "worktree_error"
    if isinstance(exc, ld.DefinitionValidationError):
        return "definition_invalid"
    if isinstance(exc, lc.InvalidStateError):
        return "invalid_state"
    return "general_error"


if __name__ == "__main__":
    sys.exit(main())
