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
import os
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


def _reexec_under_target_interpreter() -> None:
    """Re-exec this hook under $AI_ORCHESTRA_PYTHON if set (Issue #345).

    Codex CLI spawns this hook via a literal ``python3 <path>`` command in
    ``.codex/hooks.json``. Which interpreter ``python3`` resolves to depends
    on the spawn environment's PATH (e.g. a version-manager shim vs. the
    system interpreter in a non-interactive shell), which is out of this
    hook's control. When ``AI_ORCHESTRA_PYTHON`` is set to a known-good
    interpreter (``codex_run.py`` / ``codex_review.py`` set it to their own
    ``sys.executable`` before invoking ``codex exec``), re-exec under it so
    the hook body runs with a predictable interpreter regardless of how
    ``python3`` resolved.

    No-op (no behavior change) when the variable is unset, already the
    running interpreter, the target path is missing, or a re-exec already
    happened once (guarded by the sentinel below, not by path comparison,
    since version-manager shims can make the resolved path differ from
    ``sys.executable`` even after a successful re-exec).

    LIMITATIONS: this is a self-referential trust bootstrap, not a hardening
    guarantee. The decision of whether to switch interpreter runs inside the
    very PATH-resolved ``python3`` process it is trying to route around. If
    that interpreter is attacker-influenced (a version-manager shim,
    ``.python-version``, a malicious ``sitecustomize.py``, etc. -- none of
    which are covered by the ``.claude/orchestra.json`` ``codex_file_hashes``
    ledger), the attacker can patch ``os.execv``, strip
    ``AI_ORCHESTRA_PYTHON`` from the environment, or otherwise run arbitrary
    code before this function is ever reached, defeating the re-exec before
    it happens. This is not a new privilege escalation
    (``AI_ORCHESTRA_PYTHON`` sits at the same trust level as PATH; the
    PATH-hijack exposure predates this re-exec), but it does not make the
    hook robust against a deliberately substituted interpreter -- it only
    narrows the common case of an unexpected/stale PATH resolution. Pinning
    an absolute interpreter path directly inside ``.codex/hooks.json``
    (which *is* covered by the hash ledger) would close this remaining gap,
    but trades away the current machine-independent distribution/sync
    model; that is a follow-up trade-off decision, not part of this fix.
    """
    target = os.environ.get("AI_ORCHESTRA_PYTHON")
    if not target or os.environ.get("_AI_ORCHESTRA_HOOK_REEXECED"):
        return
    if not os.path.isfile(target):
        return
    if os.path.realpath(target) == os.path.realpath(sys.executable):
        return
    os.environ["_AI_ORCHESTRA_HOOK_REEXECED"] = "1"
    try:
        os.execv(target, [target, *sys.argv])
    except OSError:
        pass


if __name__ == "__main__":
    _reexec_under_target_interpreter()
    sys.exit(main())
