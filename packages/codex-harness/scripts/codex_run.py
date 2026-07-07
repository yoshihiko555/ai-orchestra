#!/usr/bin/env python3
"""Non-interactive Codex CLI task runner (Stage 1 harness).

Implements docs/design/codex-cli-harness.md §8.2 / §9: runs a task prompt
through ``codex exec --json`` and saves a full artifact set under
``.codex/runs/<run_id>/``.

Usage:
    python3 codex_run.py "<task>" [--project <root>] \\
        [--sandbox workspace-write|read-only] [--allow-untrusted-hooks] \\
        [--timeout <seconds>]
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from harness_common import (  # noqa: E402
    check_required_codex_files,
    coerce_validation_timeout,
    find_repo_root,
    is_ledger_entry_trusted_against_hashes,
    load_codex_file_hashes,
    redact_files_in_place,
    redact_secrets,
    resolve_trust_flags,
    run_version_gate,
    verify_hooks_trust_against_hashes,
    write_atomic,
)
from harness_common import parse_events as _shared_parse_events  # noqa: E402

LABEL = "codex-run"
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_VALIDATION_TIMEOUT_SECONDS = 300
SLUG_MAX_LENGTH = 40
SCHEMA_REL_PATH = ".codex/schemas/task_result.schema.json"
VALIDATION_TARGET_REL = ".codex/validation.json"
REQUIRED_CODEX_FILES = [
    ".codex/hooks.json",
    ".codex/config.toml",
    SCHEMA_REL_PATH,
    VALIDATION_TARGET_REL,
]
FINAL_SCHEMA_REQUIRED_KEYS = {"status", "summary", "files_changed", "validation", "risks"}
UNTRUSTED_VALIDATION_SUMMARY = "skipped (untrusted validation.json)"


def slugify(task: str) -> str:
    """Build a filesystem-safe slug from a task string for the run dir name."""
    lowered = task.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug[:SLUG_MAX_LENGTH] or "task"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a non-interactive Codex CLI task with artifact capture."
    )
    parser.add_argument("task", help="Task prompt to pass to `codex exec`")
    parser.add_argument("--project", default=".", help="Project root (default: cwd)")
    parser.add_argument(
        "--sandbox", choices=["workspace-write", "read-only"], default="workspace-write"
    )
    parser.add_argument("--allow-untrusted-hooks", action="store_true")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    return parser.parse_args(argv)


def run_git(repo_root: Path, args: list[str]) -> str:
    """Run a git subcommand and return its trimmed stdout (empty on error)."""
    try:
        completed = subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip()


def preflight(repo_root: Path, allow_untrusted: bool) -> list[str] | None:
    """Run version gate + required-file check + hook trust verification.

    Returns the extra `codex exec` flags, or None if the run must abort
    (an error has already been printed).
    """
    if not run_version_gate(LABEL):
        return None

    missing = check_required_codex_files(repo_root, REQUIRED_CODEX_FILES)
    if missing:
        print(
            f"[{LABEL}] error: missing required .codex files: {', '.join(missing)}", file=sys.stderr
        )
        return None

    return resolve_trust_flags(repo_root, allow_untrusted, LABEL)


def build_metadata(
    run_id: str, repo_root: Path, sandbox: str, approval_policy: str, started_at: str
) -> dict[str, Any]:
    """Assemble metadata.json content per docs/design/codex-cli-harness.md §9.2."""
    head_ref = run_git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    base_symbolic = run_git(repo_root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    base_ref = base_symbolic.rsplit("/", 1)[-1] if base_symbolic else "main"
    return {
        "run_id": run_id,
        "agent_provider": "codex-cli",
        "mode": "noninteractive",
        "repo_root": str(repo_root),
        "base_ref": base_ref,
        "head_ref": head_ref,
        "model": "",
        "sandbox": sandbox,
        "approval_policy": approval_policy,
        "started_at": started_at,
    }


def execute_codex(
    repo_root: Path,
    run_dir: Path,
    task: str,
    sandbox: str,
    trust_flags: list[str],
    timeout: int,
) -> int:
    """Run `codex exec --json`, streaming stdout/stderr to run dir artifacts."""
    cmd = [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        sandbox,
        # NOTE: `codex exec` (0.142.x) has no --ask-for-approval flag; exec is
        # non-interactive and never prompts (verified via E2E, see Plans.md).
        # Pin approval_policy=never so this non-interactive run keeps a strict,
        # no-escalation sandbox regardless of the interactive default in
        # `.codex/config.toml` (which is `on-failure` for human-approved runs).
        "-c",
        "approval_policy=never",
        "--output-schema",
        SCHEMA_REL_PATH,
        *trust_flags,
        task,
    ]
    events_path = run_dir / "events.jsonl"
    progress_path = run_dir / "progress.log"
    # events.jsonl / progress.log are live subprocess stream targets (not
    # pre-computed content), so they are written raw here and redacted in a
    # post-run pass instead (see `_redact_run_artifacts_in_place`, called
    # from main() once the subprocess has completed).
    with (
        open(events_path, "w", encoding="utf-8") as events_f,
        open(progress_path, "w", encoding="utf-8") as progress_f,
    ):
        try:
            completed = subprocess.run(
                cmd,
                cwd=repo_root,
                stdin=subprocess.DEVNULL,
                stdout=events_f,
                stderr=progress_f,
                text=True,
                timeout=timeout,
            )
            return completed.returncode
        except subprocess.TimeoutExpired:
            progress_f.write(f"\n[{LABEL}] error: codex exec timed out after {timeout}s\n")
            return 124


def capture_git_status(repo_root: Path, run_dir: Path, label: str) -> None:
    status = run_git(repo_root, ["status", "--porcelain"])
    write_atomic(run_dir / f"git-status.{label}.txt", status + ("\n" if status else ""))


def capture_diff(repo_root: Path, run_dir: Path) -> None:
    diff_stat = run_git(repo_root, ["diff", "--stat"])
    write_atomic(run_dir / "diff-stat.txt", diff_stat + ("\n" if diff_stat else ""))

    try:
        completed = subprocess.run(
            ["git", "diff", "--binary"], cwd=repo_root, capture_output=True, text=True, timeout=60
        )
        diff_patch = completed.stdout
    except (OSError, subprocess.TimeoutExpired):
        diff_patch = ""
    write_atomic(run_dir / "diff.patch", redact_secrets(diff_patch))


def run_validation(
    repo_root: Path, run_dir: Path, pre_run_hashes: dict[str, str] | None
) -> list[dict[str, str]]:
    """Run .codex/validation.json commands and write a combined validation.log.

    Before running anything, the hook/rule/validation files are checked against
    the immutable hash snapshot captured before ``codex exec`` started. This
    prevents a workspace-write task from editing both ``validation.json`` and
    ``.claude/orchestra.json`` during the run and making a malicious validation
    command appear trusted after the fact.
    """
    validation_path = repo_root / ".codex" / "validation.json"
    if not validation_path.is_file():
        return []

    trust = verify_hooks_trust_against_hashes(repo_root, pre_run_hashes)
    if not trust.trusted or not is_ledger_entry_trusted_against_hashes(
        repo_root, VALIDATION_TARGET_REL, pre_run_hashes
    ):
        result = {
            "command": "(validation.json)",
            "status": "skipped",
            "summary": UNTRUSTED_VALIDATION_SUMMARY,
        }
        reasons = "; ".join(trust.reasons) if trust.reasons else UNTRUSTED_VALIDATION_SUMMARY
        log_block = f"=== [SKIPPED] validation.json ===\n{UNTRUSTED_VALIDATION_SUMMARY}: {reasons}"
        write_atomic(run_dir / "validation.log", redact_secrets(log_block))
        return [result]

    try:
        data = json.loads(validation_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    commands = data.get("commands", [])
    if not isinstance(commands, list):
        return []

    results: list[dict[str, str]] = []
    log_lines: list[str] = []
    for entry in commands:
        result, log_block = _run_validation_command(entry, repo_root)
        results.append(result)
        log_lines.append(log_block)

    write_atomic(run_dir / "validation.log", redact_secrets("\n".join(log_lines)))
    return results


def _run_validation_command(entry: Any, repo_root: Path) -> tuple[dict[str, str], str]:
    """Run a single validation command entry. Returns (result dict, log block).

    `entry` is untrusted input from `.codex/validation.json`. Malformed
    entries (not a dict, non-string `command`, non-numeric `timeout`) are
    converted into a failed result instead of raising. Mirrors the same
    hardening in
    packages/codex-harness/codex/hooks/stop_validate.py::run_command.
    """
    if not isinstance(entry, dict):
        command = repr(entry)
        output = "invalid validation entry: not an object"
        result = {"command": command, "status": "failed", "summary": output[:2000]}
        log_block = f"=== [FAILED] {command} ===\n{output}"
        return result, log_block

    raw_command = entry.get("command", "")
    if not isinstance(raw_command, str):
        command = repr(raw_command)
        output = "invalid validation entry: command is not a string"
        result = {"command": command, "status": "failed", "summary": output[:2000]}
        log_block = f"=== [FAILED] {command} ===\n{output}"
        return result, log_block

    command = raw_command
    timeout = coerce_validation_timeout(
        entry.get("timeout", DEFAULT_VALIDATION_TIMEOUT_SECONDS),
        DEFAULT_VALIDATION_TIMEOUT_SECONDS,
    )
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        passed = False
        output = f"invalid command syntax: {exc}"
        status = "failed"
        result = {"command": command, "status": status, "summary": output[:2000]}
        log_block = f"=== [{status.upper()}] {command} ===\n{output}"
        return result, log_block

    if not argv:
        output = "invalid validation entry: command is empty"
        status = "failed"
        result = {"command": command, "status": status, "summary": output[:2000]}
        log_block = f"=== [{status.upper()}] {command} ===\n{output}"
        return result, log_block

    try:
        completed = subprocess.run(
            argv,
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        passed = completed.returncode == 0
        output = completed.stdout + completed.stderr
    except subprocess.TimeoutExpired:
        passed = False
        output = "timed out"
    except OSError as exc:
        passed = False
        output = str(exc)

    status = "passed" if passed else "failed"
    result = {"command": command, "status": status, "summary": output[:2000]}
    log_block = f"=== [{status.upper()}] {command} ===\n{output}"
    return result, log_block


def _build_task_fallback(status: str, summary: str) -> dict[str, Any]:
    """Build the fallback task_result payload shape (see FINAL_SCHEMA_REQUIRED_KEYS)."""
    return {
        "status": status,
        "summary": summary,
        "files_changed": [],
        "validation": [],
        "risks": [],
    }


def parse_events(events_path: Path, codex_returncode: int) -> dict[str, Any]:
    """Extract the final structured result from events.jsonl.

    Thin wrapper around harness_common.parse_events with the task_result
    schema's required keys and fallback shape.
    """
    return _shared_parse_events(
        events_path, codex_returncode, FINAL_SCHEMA_REQUIRED_KEYS, _build_task_fallback
    )


def build_report(
    final_result: dict[str, Any],
    validation_results: list[dict[str, str]],
    exit_code: int,
    duration_seconds: float,
) -> str:
    lines = [
        "# Codex Run Report",
        "",
        f"- exit code: {exit_code}",
        f"- duration: {duration_seconds:.1f}s",
        "",
        "## Summary",
        final_result.get("summary") or "(empty)",
        "",
        "## Files changed",
        *_format_files_changed(final_result.get("files_changed") or []),
        "",
        "## Validation",
        *_format_validation(validation_results),
        "",
        "## Risks",
        *_format_risks(final_result.get("risks") or []),
    ]
    return "\n".join(lines) + "\n"


def _format_files_changed(files_changed: list[dict[str, Any]]) -> list[str]:
    if not files_changed:
        return ["(none reported)"]
    return [
        f"- `{item.get('path', '?')}` ({item.get('change_type', '?')}): {item.get('notes', '')}"
        for item in files_changed
    ]


def _format_validation(results: list[dict[str, str]]) -> list[str]:
    if not results:
        return ["(no validation commands configured)"]
    return [f"- [{r['status']}] {r['command']}" for r in results]


def _format_risks(risks: list[dict[str, Any]]) -> list[str]:
    if not risks:
        return ["(none reported)"]
    return [
        f"- [{r.get('severity', '?')}] {r.get('description', '')} — {r.get('mitigation', '')}"
        for r in risks
    ]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    project_root = Path(args.project).resolve()
    repo_root = find_repo_root(project_root)
    if repo_root is None:
        print(f"[{LABEL}] error: {project_root} is not inside a git repository", file=sys.stderr)
        return 1

    trust_flags = preflight(repo_root, args.allow_untrusted_hooks)
    if trust_flags is None:
        return 1
    pre_run_hashes = load_codex_file_hashes(repo_root)

    started_at_dt = datetime.datetime.now().astimezone()
    run_id = f"{started_at_dt.strftime('%Y%m%d-%H%M%S')}-{slugify(args.task)}"
    run_dir = repo_root / ".codex" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    write_atomic(run_dir / "prompt.md", redact_secrets(args.task + "\n"))
    metadata = build_metadata(run_id, repo_root, args.sandbox, "never", started_at_dt.isoformat())
    write_atomic(run_dir / "metadata.json", redact_secrets(json.dumps(metadata, indent=2) + "\n"))

    capture_git_status(repo_root, run_dir, "before")
    exit_code = execute_codex(
        repo_root, run_dir, args.task, args.sandbox, trust_flags, args.timeout
    )
    redact_files_in_place(run_dir / "events.jsonl", run_dir / "progress.log")
    capture_git_status(repo_root, run_dir, "after")
    capture_diff(repo_root, run_dir)

    validation_results = run_validation(repo_root, run_dir, pre_run_hashes)
    final_result = parse_events(run_dir / "events.jsonl", exit_code)
    write_atomic(run_dir / "final.json", redact_secrets(json.dumps(final_result, indent=2) + "\n"))

    duration_seconds = (datetime.datetime.now().astimezone() - started_at_dt).total_seconds()
    report = build_report(final_result, validation_results, exit_code, duration_seconds)
    write_atomic(run_dir / "report.md", redact_secrets(report))

    print(f"[{LABEL}] run artifacts: {run_dir}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
