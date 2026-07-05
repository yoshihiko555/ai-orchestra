#!/usr/bin/env python3
"""Stop hook: run deterministic validation commands and report failures.

Reads ``.codex/validation.json`` (relative to the hook payload's ``cwd``)
for a list of ``{"command": ..., "timeout": ...}`` entries, runs them in
order, and writes a combined log to
``.codex/reports/validation-<timestamp>.log``.

This hook never blocks Stop: it always exits 0. When one or more
commands fail, a summary is emitted as a ``systemMessage`` in the JSON
written to stdout so the failure is visible without stopping the agent.

If ``.codex/validation.json`` does not exist, or defines no commands,
the hook does nothing and exits 0.

Before running anything, ``.codex/validation.json`` itself is checked
against the sync ledger (``.claude/orchestra.json``'s
``codex_file_hashes``) so an agent cannot smuggle in arbitrary commands
by rewriting the validation list. This mirrors (but does not import)
``packages/codex-harness/scripts/harness_common.py::verify_hooks_trust``:
this file is a standalone distribution artifact with no dependency on
``scripts/`` at runtime, so the hash check is duplicated in miniature
here (see ``is_validation_json_trusted`` below).
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 300
VALIDATION_RELATIVE_PATH = Path(".codex/validation.json")
REPORTS_RELATIVE_DIR = Path(".codex/reports")
ORCHESTRA_JSON_RELATIVE_PATH = Path(".claude/orchestra.json")
TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
UNTRUSTED_VALIDATION_MESSAGE = (
    "validation.json is not trusted (modified or unregistered); skipping validation"
)
# Cumulative wall-clock budget for *all* validation commands combined,
# deliberately kept below hooks.json's Stop hook timeout (600s as of this
# writing) so this hook has headroom to write its log and exit cleanly
# instead of being killed mid-run. Individual per-command `timeout` entries
# already bound a single command, but not their sum.
TOTAL_BUDGET_SECONDS = 540
BUDGET_EXCEEDED_MESSAGE = "skipped: validation time budget exceeded"

# Mirrors packages/codex-harness/codex/hooks/user_prompt_secret_scan.py
# SECRET_PATTERNS and packages/codex-harness/scripts/harness_common.py
# REDACTION_PATTERNS. Kept as a separate constant (not a shared import) so
# this distributed hook file has no dependency on scripts/ at runtime.
_MIN_TOKEN_LENGTH = 20
_MIN_GENERIC_KEY_LENGTH = 10

REDACTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OPENAI_API_KEY assignment", re.compile(r"OPENAI_API_KEY\s*=\s*\S+", re.IGNORECASE)),
    ("AWS_ACCESS_KEY_ID value", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "AWS_SECRET_ACCESS_KEY assignment",
        re.compile(r"AWS_SECRET_ACCESS_KEY\s*=\s*\S+", re.IGNORECASE),
    ),
    ("GITHUB_TOKEN assignment", re.compile(r"GITHUB_TOKEN\s*=\s*\S+", re.IGNORECASE)),
    ("GitHub PAT (ghp_)", re.compile(rf"\bghp_[A-Za-z0-9]{{{_MIN_TOKEN_LENGTH},}}")),
    (
        "GitHub fine-grained PAT (github_pat_)",
        re.compile(rf"\bgithub_pat_[A-Za-z0-9_]{{{_MIN_TOKEN_LENGTH},}}"),
    ),
    ("API key (sk- prefix)", re.compile(rf"\bsk-[A-Za-z0-9]{{{_MIN_GENERIC_KEY_LENGTH},}}")),
    (
        "PEM private key block",
        re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----[\s\S]*?-----END[ A-Z]*PRIVATE KEY-----"),
    ),
]


def redact_secrets(text: str) -> str:
    """Replace secret-like substrings with `[REDACTED:<pattern name>]`."""
    result = text
    for name, pattern in REDACTION_PATTERNS:
        result = pattern.sub(f"[REDACTED:{name}]", result)
    return result


def read_stdin_payload() -> dict[str, Any] | None:
    """Parse the hook payload from stdin. Returns None on any parse failure."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def resolve_cwd(payload: dict[str, Any] | None) -> Path:
    """Resolve the repo working directory from the payload, falling back to cwd."""
    if payload is not None:
        value = payload.get("cwd")
        if isinstance(value, str) and value:
            return Path(value)
    return Path.cwd()


def resolve_repo_root(cwd: Path) -> Path:
    """Resolve the project root, falling back to `git rev-parse --show-toplevel`.

    The Stop hook payload's ``cwd`` is expected to already be the project
    root (see module docstring). This fallback exists only for robustness
    in case a caller passes a subdirectory: it never raises and always
    returns a usable path (``cwd`` itself when git can't resolve one).
    """
    if (cwd / ".claude").is_dir() or (cwd / ".git").exists():
        return cwd
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return cwd
    if completed.returncode != 0:
        return cwd
    toplevel = completed.stdout.strip()
    return Path(toplevel) if toplevel else cwd


def _load_recorded_validation_hash(root: Path) -> str | None:
    """Read the recorded SHA-256 for .codex/validation.json from the sync ledger."""
    orchestra_path = root / ORCHESTRA_JSON_RELATIVE_PATH
    if not orchestra_path.is_file():
        return None
    try:
        data = json.loads(orchestra_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    hashes = data.get("codex_file_hashes")
    if not isinstance(hashes, dict):
        return None
    value = hashes.get(VALIDATION_RELATIVE_PATH.as_posix())
    return value if isinstance(value, str) else None


def is_validation_json_trusted(root: Path) -> bool:
    """Check .codex/validation.json's SHA-256 against the sync ledger.

    Fail-closed: a missing ledger, missing ledger entry, missing file,
    symlink, or hash mismatch is all treated as untrusted. See the module
    docstring for how this relates to
    harness_common.verify_hooks_trust().
    """
    validation_path = root / VALIDATION_RELATIVE_PATH
    if validation_path.is_symlink() or not validation_path.is_file():
        return False

    recorded_hash = _load_recorded_validation_hash(root)
    if recorded_hash is None:
        return False

    try:
        current_hash = hashlib.sha256(validation_path.read_bytes()).hexdigest()
    except OSError:
        return False
    return current_hash == recorded_hash


def load_commands(root: Path) -> list[dict[str, Any]]:
    """Load the validation command list from .codex/validation.json."""
    path = root / VALIDATION_RELATIVE_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    commands = data.get("commands", [])
    return commands if isinstance(commands, list) else []


def _coerce_timeout(value: Any) -> int | float:
    """Best-effort int conversion for a validation entry's `timeout` field.

    Falls back to DEFAULT_TIMEOUT_SECONDS when `value` is missing, not a
    number, and not a numeric string (e.g. a bool, a list, or "soon").
    """
    if isinstance(value, bool):
        return DEFAULT_TIMEOUT_SECONDS
    if isinstance(value, (int, float)):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def run_command(entry: dict[str, Any], cwd: Path) -> dict[str, Any]:
    """Run a single validation command entry and capture its result.

    `entry` is untrusted input from `.codex/validation.json` (though the
    file itself is hash-verified by `is_validation_json_trusted` before
    `main` gets here). Malformed entries (not a dict, non-string
    `command`, non-numeric `timeout`) are converted into a failed result
    instead of raising, so one bad entry can't crash the whole hook.
    """
    if not isinstance(entry, dict):
        return {
            "command": repr(entry),
            "passed": False,
            "output": "invalid validation entry: not an object",
        }

    raw_command = entry.get("command", "")
    if not isinstance(raw_command, str):
        return {
            "command": repr(raw_command),
            "passed": False,
            "output": "invalid validation entry: command is not a string",
        }
    command = raw_command
    timeout = _coerce_timeout(entry.get("timeout", DEFAULT_TIMEOUT_SECONDS))

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return {"command": command, "passed": False, "output": f"invalid command syntax: {exc}"}

    if not argv:
        return {
            "command": command,
            "passed": False,
            "output": "invalid validation entry: command is empty",
        }

    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"command": command, "passed": False, "output": "timed out"}
    except OSError as exc:
        return {"command": command, "passed": False, "output": str(exc)}

    passed = completed.returncode == 0
    output = completed.stdout + completed.stderr
    return {"command": command, "passed": passed, "output": output}


def run_commands_with_budget(
    commands: list[dict[str, Any]],
    cwd: Path,
    total_budget_seconds: float = TOTAL_BUDGET_SECONDS,
) -> list[dict[str, Any]]:
    """Run `commands` in order, enforcing a cumulative wall-clock budget.

    Individual commands each have their own `timeout`, but nothing
    previously bounded their *sum*: a long enough command list could exceed
    the Stop hook's overall timeout and be killed mid-run before writing
    its log. Once `total_budget_seconds` has elapsed, remaining commands
    are reported as skipped (not run) rather than executed anyway.
    """
    results: list[dict[str, Any]] = []
    started_at = time.monotonic()
    budget_exhausted = False

    for entry in commands:
        if not budget_exhausted and (time.monotonic() - started_at) >= total_budget_seconds:
            budget_exhausted = True

        if budget_exhausted:
            raw_command = entry.get("command", "") if isinstance(entry, dict) else entry
            command = raw_command if isinstance(raw_command, str) else repr(raw_command)
            results.append({"command": command, "passed": False, "output": BUDGET_EXCEEDED_MESSAGE})
            continue

        results.append(run_command(entry, cwd))

    return results


def write_log(root: Path, results: list[dict[str, Any]]) -> Path:
    """Write a combined validation log and return its path."""
    reports_dir = root / REPORTS_RELATIVE_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
    log_path = reports_dir / f"validation-{timestamp}.log"

    lines = []
    for result in results:
        status = "PASSED" if result["passed"] else "FAILED"
        lines.append(f"=== [{status}] {result['command']} ===")
        lines.append(result["output"])
    log_path.write_text(redact_secrets("\n".join(lines)), encoding="utf-8")
    return log_path


def build_summary(results: list[dict[str, Any]]) -> str | None:
    """Build a one-line failure summary, or None if everything passed."""
    failed = [r["command"] for r in results if not r["passed"]]
    if not failed:
        return None
    return f"Validation failed: {', '.join(failed)}"


def main() -> int:
    payload = read_stdin_payload()
    cwd = resolve_cwd(payload)
    repo_root = resolve_repo_root(cwd)

    if not is_validation_json_trusted(repo_root):
        print(json.dumps({"continue": True, "systemMessage": UNTRUSTED_VALIDATION_MESSAGE}))
        return 0

    commands = load_commands(repo_root)
    if not commands:
        return 0

    results = run_commands_with_budget(commands, repo_root)
    write_log(repo_root, results)

    summary = build_summary(results)
    if summary is not None:
        print(json.dumps({"continue": True, "systemMessage": summary}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
