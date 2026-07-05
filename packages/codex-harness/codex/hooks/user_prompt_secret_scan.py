#!/usr/bin/env python3
"""UserPromptSubmit hook: block prompts that contain secret-like patterns.

Reads the Codex hook payload (JSON) from stdin, scans the ``prompt`` field
for common secret patterns (API keys, tokens, private key blocks), and
exits with code 2 (block) if any pattern is detected. Only the matched
pattern *name* is reported on stderr; the matched value itself is never
echoed back.

Fail-open: if stdin cannot be parsed as JSON, or no ``prompt`` field is
present, the hook exits 0 (allow) rather than blocking.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

MIN_TOKEN_LENGTH = 20
MIN_GENERIC_KEY_LENGTH = 10

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OPENAI_API_KEY assignment", re.compile(r"OPENAI_API_KEY\s*=", re.IGNORECASE)),
    ("AWS_ACCESS_KEY_ID", re.compile(r"AWS_ACCESS_KEY_ID", re.IGNORECASE)),
    ("AWS_SECRET_ACCESS_KEY", re.compile(r"AWS_SECRET_ACCESS_KEY", re.IGNORECASE)),
    ("GITHUB_TOKEN", re.compile(r"GITHUB_TOKEN", re.IGNORECASE)),
    ("GitHub PAT (ghp_)", re.compile(rf"\bghp_[A-Za-z0-9]{{{MIN_TOKEN_LENGTH},}}")),
    (
        "GitHub fine-grained PAT (github_pat_)",
        re.compile(rf"\bgithub_pat_[A-Za-z0-9_]{{{MIN_TOKEN_LENGTH},}}"),
    ),
    ("API key (sk- prefix)", re.compile(rf"\bsk-[A-Za-z0-9]{{{MIN_GENERIC_KEY_LENGTH},}}")),
    ("PEM private key block", re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----")),
]


def read_stdin_payload() -> dict[str, Any] | None:
    """Parse the hook payload from stdin. Returns None on any parse failure."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def extract_prompt(payload: dict[str, Any]) -> str:
    """Extract the user prompt text from the hook payload."""
    value = payload.get("prompt", "")
    return value if isinstance(value, str) else ""


def find_matches(prompt: str) -> list[str]:
    """Return the names of all secret patterns found in the prompt."""
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(prompt)]


def main() -> int:
    payload = read_stdin_payload()
    if payload is None:
        return 0

    prompt = extract_prompt(payload)
    if not prompt:
        return 0

    matches = find_matches(prompt)
    if not matches:
        return 0

    joined = ", ".join(matches)
    print(f"[codex-harness] Secret pattern detected in prompt: {joined}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
