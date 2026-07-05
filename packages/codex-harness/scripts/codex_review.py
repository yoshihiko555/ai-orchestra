#!/usr/bin/env python3
"""Read-only Codex CLI review runner (Stage 2 harness).

Implements docs/design/codex-cli-harness.md §8.3 / §11.3: a local,
pre-PR self-check that feeds `git diff <base>...HEAD` to `codex exec`
in read-only sandbox mode and saves structured findings.

This is distinct from the existing `/review` skill and PR auto-review
(#141): those run against a live PR; this is a local, human-initiated
self-check before opening one.

Usage:
    python3 codex_review.py [--base main] [--project <root>] \\
        [--allow-untrusted-hooks] [--timeout <seconds>]
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from harness_common import (  # noqa: E402
    check_required_codex_files,
    find_repo_root,
    redact_files_in_place,
    redact_secrets,
    resolve_trust_flags,
    run_version_gate,
    write_atomic,
)
from harness_common import parse_events as _shared_parse_events  # noqa: E402

LABEL = "codex-review"
DEFAULT_TIMEOUT_SECONDS = 600
SCHEMA_REL_PATH = ".codex/schemas/review_result.schema.json"
REQUIRED_CODEX_FILES = [
    ".codex/hooks.json",
    ".codex/config.toml",
    SCHEMA_REL_PATH,
]
FINAL_SCHEMA_REQUIRED_KEYS = {"status", "summary", "findings"}
REVIEW_PROMPT = (
    "Review the diff provided on stdin. Focus on correctness, security, test"
    " coverage, and maintainability. Return only structured findings."
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a read-only Codex CLI review of the diff against a base branch."
    )
    parser.add_argument("--base", default="main", help="Base branch/ref (default: main)")
    parser.add_argument("--project", default=".", help="Project root (default: cwd)")
    parser.add_argument("--allow-untrusted-hooks", action="store_true")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    return parser.parse_args(argv)


def run_git(repo_root: Path, args: list[str], timeout: int = 30) -> tuple[str, int | None]:
    """Run a git subcommand. Returns ``(stdout, returncode)``.

    ``returncode`` is ``None`` if the subprocess itself could not be
    launched or timed out (git missing, timeout expired) -- callers should
    treat this the same as a non-zero exit. A real git failure (e.g. an
    unknown/missing ``--base`` ref) surfaces as a non-zero ``returncode``,
    which callers can distinguish from a genuinely empty diff
    (``returncode == 0`` and empty stdout). stderr is not returned here;
    callers that need a diagnostic message on failure should re-run with
    ``capture_output`` themselves or just report the ref/args used.
    """
    try:
        completed = subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", None
    return completed.stdout, completed.returncode


def preflight(repo_root: Path, allow_untrusted: bool) -> list[str] | None:
    """Run version gate + required-file check + hook trust verification."""
    if not run_version_gate(LABEL):
        return None

    missing = check_required_codex_files(repo_root, REQUIRED_CODEX_FILES)
    if missing:
        print(
            f"[{LABEL}] error: missing required .codex files: {', '.join(missing)}", file=sys.stderr
        )
        return None

    return resolve_trust_flags(repo_root, allow_untrusted, LABEL)


def build_run_dir(repo_root: Path) -> Path:
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = repo_root / ".codex" / "runs" / f"{timestamp}-review"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def execute_codex_review(
    repo_root: Path,
    run_dir: Path,
    input_diff_path: Path,
    trust_flags: list[str],
    timeout: int,
) -> int:
    """Run `codex exec --json --sandbox read-only`, feeding the diff via stdin."""
    cmd = [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        # NOTE: `codex exec` (0.142.x) has no --ask-for-approval flag; exec is
        # non-interactive and never prompts (verified via E2E, see Plans.md).
        "--output-schema",
        SCHEMA_REL_PATH,
        *trust_flags,
        REVIEW_PROMPT,
    ]
    events_path = run_dir / "events.jsonl"
    progress_path = run_dir / "progress.log"
    with (
        open(input_diff_path, encoding="utf-8") as diff_f,
        open(events_path, "w", encoding="utf-8") as events_f,
        open(progress_path, "w", encoding="utf-8") as progress_f,
    ):
        try:
            completed = subprocess.run(
                cmd,
                cwd=repo_root,
                stdin=diff_f,
                stdout=events_f,
                stderr=progress_f,
                text=True,
                timeout=timeout,
            )
            return completed.returncode
        except subprocess.TimeoutExpired:
            progress_f.write(f"\n[{LABEL}] error: codex exec timed out after {timeout}s\n")
            return 124


def _build_review_fallback(status: str, summary: str) -> dict[str, Any]:
    """Build the fallback review_result payload shape (see FINAL_SCHEMA_REQUIRED_KEYS)."""
    return {"status": status, "summary": summary, "findings": []}


def parse_events(events_path: Path, codex_returncode: int) -> dict[str, Any]:
    """Extract the final structured review result from events.jsonl.

    Thin wrapper around harness_common.parse_events with the review_result
    schema's required keys and fallback shape.
    """
    return _shared_parse_events(
        events_path, codex_returncode, FINAL_SCHEMA_REQUIRED_KEYS, _build_review_fallback
    )


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def build_report(review_result: dict[str, Any], write_warning: str | None) -> str:
    findings = sorted(
        review_result.get("findings") or [],
        key=lambda f: _SEVERITY_ORDER.get(f.get("severity", "low"), 99),
    )
    lines = [
        "# Codex Review Report",
        "",
        f"- status: {review_result.get('status', 'unknown')}",
        "",
        "## Summary",
        review_result.get("summary") or "(empty)",
        "",
        "## Findings",
    ]
    if not findings:
        lines.append("(none reported)")
    else:
        lines.extend(_format_finding(f) for f in findings)
    if write_warning:
        lines += ["", "## Warnings", f"- {write_warning}"]
    return "\n".join(lines) + "\n"


def _format_finding(finding: dict[str, Any]) -> str:
    severity = finding.get("severity", "?")
    file = finding.get("file", "?")
    line = finding.get("line")
    rationale = finding.get("rationale", "")
    suggested_fix = finding.get("suggested_fix", "")
    location = f"{file}:{line}" if line is not None else file
    return f"- [{severity}] `{location}` — {rationale} (fix: {suggested_fix})"


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

    run_dir = build_run_dir(repo_root)
    diff_text, diff_returncode = run_git(repo_root, ["diff", f"{args.base}...HEAD"], timeout=60)
    if diff_returncode != 0:
        print(
            f"[{LABEL}] error: `git diff {args.base}...HEAD` failed (exit"
            f" {diff_returncode}); is '{args.base}' a valid ref?",
            file=sys.stderr,
        )
        return 1

    input_diff_path = run_dir / "input.diff"
    # Redact before persisting *and* before feeding the diff to `codex exec`
    # via stdin, so secret-like substrings are never sent to the external
    # CLI or left recoverable in the saved artifact.
    write_atomic(input_diff_path, redact_secrets(diff_text))

    if not diff_text.strip():
        print(f"[{LABEL}] no changes between {args.base} and HEAD; nothing to review")
        return 0

    status_before, _ = run_git(repo_root, ["status", "--porcelain"])
    exit_code = execute_codex_review(repo_root, run_dir, input_diff_path, trust_flags, args.timeout)
    redact_files_in_place(run_dir / "events.jsonl", run_dir / "progress.log")
    status_after, _ = run_git(repo_root, ["status", "--porcelain"])

    write_warning = None
    if status_before != status_after:
        write_warning = (
            "git status changed during a read-only review run"
            " (unexpected write detected; investigate hooks/config)"
        )
        print(f"[{LABEL}] warning: {write_warning}", file=sys.stderr)

    review_result = parse_events(run_dir / "events.jsonl", exit_code)
    write_atomic(
        run_dir / "review.json", redact_secrets(json.dumps(review_result, indent=2) + "\n")
    )
    report = build_report(review_result, write_warning)
    write_atomic(run_dir / "report.md", redact_secrets(report))

    print(f"[{LABEL}] review artifacts: {run_dir}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
