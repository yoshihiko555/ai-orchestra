#!/usr/bin/env python3
"""PreToolUse hook: block a fixed set of dangerous shell commands.

Reads the Codex hook payload (JSON) from stdin, extracts a shell-like
command string from ``tool_input``, and blocks (exit 2) when it matches
one of the deterministic forbidden patterns. Complements
``.codex/rules/codex-harness.rules`` for conditions that are easier to
express as regex than as prefix rules (e.g. narrow ``rm -rf /`` vs. the
broader ``rm -rf`` prefix rule).

Word boundaries are used throughout to avoid false positives, e.g.
``rm -rf ./build`` is intentionally allowed by this hook (the rules file
still applies its own broader ``rm -rf`` policy).

Fail-open: if stdin cannot be parsed as JSON, or no command-like field is
present, the hook exits 0 (allow) rather than blocking.

LIMITATIONS: this hook is a regex-based text scan over a best-effort
string extraction of ``tool_input``. It cannot see shell expansion,
environment variable substitution, alias definitions, or command
composition performed by the shell itself (e.g. ``$(echo push)``,
``alias p=push``, or indirection through a wrapper script). It is a
supplementary, defense-in-depth layer, not the primary control: the
native Codex rules file (``.codex/rules/codex-harness.rules``) and the
sandbox's filesystem/network policy are the primary defenses. Do not
rely on this hook alone to prevent a determined bypass.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

COMMAND_LIKE_KEYS = ("command", "cmd", "script")

# Allows a bounded number of option/flag tokens (e.g. ``-C ..``,
# ``--no-pager``, ``--git-dir=/path``) to appear between a base command
# and its subcommand, so that flag insertion cannot be used to dodge the
# plain ``\bgit\s+push\b``-style patterns below (e.g. ``git -C .. push``).
# This is intentionally narrow: it only skips tokens that look like
# options (start with ``-``), not arbitrary subcommands, to avoid
# matching across unrelated command chains (e.g. ``git log | grep push``).
_OPTION_TOKENS = r"(?:-\S+(?:\s+(?!-)\S+)?\s+){0,4}"

# Spelling/ordering variants of the `-rf` flag pair that a plain `-rf`
# literal would miss: `-fr` (reversed short flags), `-r -f` / `-f -r`
# (split short flags), and the long-form `--recursive --force` (either
# order). Used only for the narrow root/home-targeted patterns below; the
# broader `rm -rf` (any target) prefix policy lives in
# `.codex/rules/codex-harness.rules`.
_RM_RF_FLAGS = r"(?:-rf|-fr|-r\s+-f|-f\s+-r|--recursive\s+--force|--force\s+--recursive)"

FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("git push", re.compile(rf"\bgit\s+{_OPTION_TOKENS}push\b")),
    ("gh pr merge", re.compile(rf"\bgh\s+{_OPTION_TOKENS}pr\s+merge\b")),
    ("gh release create", re.compile(rf"\bgh\s+{_OPTION_TOKENS}release\s+create\b")),
    ("npm publish", re.compile(r"\bnpm\s+publish\b")),
    ("pnpm publish", re.compile(r"\bpnpm\s+publish\b")),
    ("docker push", re.compile(rf"\bdocker\s+{_OPTION_TOKENS}push\b")),
    ("kubectl apply", re.compile(rf"\bkubectl\s+{_OPTION_TOKENS}apply\b")),
    ("terraform apply", re.compile(rf"\bterraform\s+{_OPTION_TOKENS}apply\b")),
    ("rm -rf /", re.compile(rf"\brm\s+{_RM_RF_FLAGS}\s+/(?:\s|$)")),
    ("rm -rf ~", re.compile(rf"\brm\s+{_RM_RF_FLAGS}\s+~(?:\s|/|$)")),
    ("chmod -R 777", re.compile(r"\bchmod\s+-R\s+777\b")),
    ("curl/wget piped to shell", re.compile(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sh|bash)\b")),
]


def read_stdin_payload() -> dict[str, Any] | None:
    """Parse the hook payload from stdin. Returns None on any parse failure."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def extract_command(tool_input: dict[str, Any]) -> str:
    """Extract a shell command string from a tool_input payload.

    Values may be a plain string or a list of argv tokens (e.g. Codex's
    exec-style tool calls). All candidate fields are joined so patterns
    can match regardless of the exact shape.
    """
    parts: list[str] = []
    for key in COMMAND_LIKE_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.append(" ".join(str(item) for item in value))
    return " ".join(parts)


def find_violations(command: str) -> list[str]:
    """Return the names of all forbidden patterns matched by the command."""
    return [name for name, pattern in FORBIDDEN_PATTERNS if pattern.search(command)]


def main() -> int:
    payload = read_stdin_payload()
    if payload is None:
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    command = extract_command(tool_input)
    if not command:
        return 0

    violations = find_violations(command)
    if not violations:
        return 0

    joined = ", ".join(violations)
    print(f"[codex-harness] Blocked command matching forbidden policy: {joined}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
