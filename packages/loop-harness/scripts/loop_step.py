#!/usr/bin/env python3
"""LP-1 loop-harness CLI: thin JSON adapter over loop_common."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from dataclasses import dataclass
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


@dataclass(frozen=True)
class CliFailure(Exception):
    """Structured CLI failure rendered as JSON."""

    code: str
    message: str
    exit_code: int


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
    worktree = wm.create_worktree(project, args.issue)
    try:
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
        )
    except Exception:
        _rollback_created_worktree(project, args.issue, worktree_path, worktree_existed)
        raise
    return _proposal_response(loop_id, result, "loop initialized; first action proposed")


def cmd_attach(args: argparse.Namespace) -> dict[str, Any]:
    """Handle attach."""
    project = _project_dir(args.project)
    result = lc.attach(args.loop_id, project, _owner_id(), _lp1_ttl(project))
    return _proposal_response(args.loop_id, result, "attached after stale lease")


def cmd_propose(args: argparse.Namespace) -> dict[str, Any]:
    """Handle propose."""
    project = _project_dir(args.project)
    lease_token = _required_lease_token(args)
    result = lc.propose(args.loop_id, project, lease_token)
    return _proposal_response(args.loop_id, result, "next action proposed")


def cmd_complete(args: argparse.Namespace) -> dict[str, Any]:
    """Handle complete."""
    project = _project_dir(args.project)
    lease_token = _required_lease_token(args)
    result = lc.complete(
        args.loop_id,
        project,
        args.action_id,
        args.state_version,
        _load_result(args.result),
        lease_token,
    )
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
    result = lc.propose(args.loop_id, project, resumed.lease_token)
    result_with_lease = lc.ProposeResult(
        action=result.action,
        action_id=result.action_id,
        state_version=result.state_version,
        expected_phase=result.expected_phase,
        phase=result.phase,
        iteration=result.iteration,
        context={**result.context, "lease_token": resumed.lease_token},
    )
    return _proposal_response(args.loop_id, result_with_lease, "resumed; first action proposed")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    try:
        args = build_parser().parse_args(argv)
        response = _dispatch(args)
        _write_json(response)
        return EXIT_SUCCESS
    except CliFailure as exc:
        _write_error(exc.code, exc.message)
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


def _proposal_response(loop_id: str, result: lc.ProposeResult, reason: str) -> dict[str, Any]:
    """Serialize ProposeResult to the stable CLI response shape."""
    action = lc.Action(result.action).value
    context = dict(result.context)
    lease_token = context.pop("lease_token", None)
    params = context.pop("params", {})
    if not isinstance(params, dict):
        params = {}
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
    return response


def _pending_action_id(loop_id: str, project: str) -> str | None:
    """Return the current pending action id through loop_common."""
    state = lc.load_state(loop_id, project)
    if state.pending_action is None:
        return None
    return state.pending_action.action_id


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


def _write_error(code: str, message: str) -> None:
    """Write machine-readable stdout and human-readable stderr diagnostics."""
    _write_json({"error": {"code": code, "message": message}})
    print(f"loop_step: {code}: {message}", file=sys.stderr)


def _message(exc: BaseException, fallback: str) -> str:
    """Return a safe diagnostic message."""
    return str(exc) or fallback


def _error_code_for(exc: BaseException) -> str:
    """Return a stable error code for known general errors."""
    if isinstance(exc, lc.RootResolutionError):
        return "root_resolution_error"
    if isinstance(exc, wm.WorktreeError):
        return "worktree_error"
    if isinstance(exc, ld.DefinitionValidationError):
        return "definition_invalid"
    return "general_error"


if __name__ == "__main__":
    sys.exit(main())
