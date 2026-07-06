#!/usr/bin/env python3
"""Secret redaction for meta-harness run artifacts.

Deliberate verbatim duplicate (per docs/design/meta-harness-detailed.md
Sec2-6) of the ``REDACTION_PATTERNS`` list and ``redact_secrets`` function in
``packages/codex-harness/scripts/harness_common.py``. That module is the
origin of these patterns; this module intentionally does not import it (each
package must stay self-contained), so the two pattern lists must be kept in
sync by hand. A future test should assert pattern-string equality between
``packages.codex_harness.scripts.harness_common.REDACTION_PATTERNS`` and
``REDACTION_PATTERNS`` in this module (see docs/design/meta-harness-detailed.md
Sec7 test strategy).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

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
        re.compile(
            r"-----BEGIN[ A-Z]*PRIVATE KEY-----[\s\S]*?-----END[ A-Z]*PRIVATE KEY-----",
        ),
    ),
]


def redact_secrets(text: str) -> str:
    """Replace secret-like substrings with `[REDACTED:<pattern name>]`."""
    result = text
    for name, pattern in REDACTION_PATTERNS:
        result = pattern.sub(f"[REDACTED:{name}]", result)
    return result


def write_atomic(path: Path, content: str) -> None:
    """Write `content` to `path` atomically (tmp file + os.replace).

    Same idiom as ``packages/codex-harness/scripts/harness_common.py``'s
    ``write_atomic``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def redact_file_in_place(path: Path) -> None:
    """Best-effort redaction pass for a single artifact file.

    Reads `path`, applies `redact_secrets`, and rewrites it atomically only
    if the content changed. Missing files and decode errors are ignored
    (best-effort, mirrors `redact_files_in_place` in codex-harness).
    """
    if not path.is_file():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    redacted = redact_secrets(content)
    if redacted != content:
        write_atomic(path, redacted)
