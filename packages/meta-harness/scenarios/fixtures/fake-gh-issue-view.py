#!/usr/bin/env python3
"""Deterministic offline ``gh`` fixture for issue-fix scenarios (Issue #254 batch 2).

Only ``gh issue view <number> --json ...`` is supported: it returns the fixed payload the
scenario ``setup:`` wrote to ``.meta-harness/gh-issue-fixture.json``. Any other subcommand
fails closed (non-zero exit) instead of silently succeeding or hanging on a real network call,
matching ``fake-gh.py``'s fail-closed style for issue-create scenarios.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_FIXTURE_PATH = Path(".meta-harness/gh-issue-fixture.json")


def main() -> int:
    args = sys.argv[1:]
    if args[:2] != ["issue", "view"]:
        print(f"unsupported fake gh invocation: {args}", file=sys.stderr)
        return 2
    if not _FIXTURE_PATH.is_file():
        print(f"missing gh issue fixture: {_FIXTURE_PATH}", file=sys.stderr)
        return 2
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
