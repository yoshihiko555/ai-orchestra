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
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 300
VALIDATION_RELATIVE_PATH = Path(".codex/validation.json")
REPORTS_RELATIVE_DIR = Path(".codex/reports")
TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

# Mirrors packages/codex-harness/codex/hooks/user_prompt_secret_scan.py
# SECRET_PATTERNS and packages/codex-harness/scripts/harness_common.py
# REDACTION_PATTERNS. Kept as a separate constant (not a shared import) so
# this distributed hook file has no dependency on scripts/ at runtime.
_MIN_TOKEN_LENGTH = 20
_MIN_GENERIC_KEY_LENGTH = 10

REDACTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OPENAI_API_KEY assignment", re.compile(r"OPENAI_API_KEY\s*=\s*\S+")),
    ("AWS_ACCESS_KEY_ID value", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("AWS_SECRET_ACCESS_KEY assignment", re.compile(r"AWS_SECRET_ACCESS_KEY\s*=\s*\S+")),
    ("GITHUB_TOKEN assignment", re.compile(r"GITHUB_TOKEN\s*=\s*\S+")),
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


def run_command(entry: dict[str, Any], cwd: Path) -> dict[str, Any]:
    """Run a single validation command entry and capture its result."""
    command = str(entry.get("command", ""))
    timeout = entry.get("timeout", DEFAULT_TIMEOUT_SECONDS)
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return {"command": command, "passed": False, "output": f"invalid command syntax: {exc}"}

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

    commands = load_commands(cwd)
    if not commands:
        return 0

    results = [run_command(entry, cwd) for entry in commands]
    write_log(cwd, results)

    summary = build_summary(results)
    if summary is not None:
        print(json.dumps({"continue": True, "systemMessage": summary}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
