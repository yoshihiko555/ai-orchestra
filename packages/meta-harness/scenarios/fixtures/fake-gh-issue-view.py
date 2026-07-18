#!/usr/bin/env python3
"""Deterministic offline ``gh`` fixture for issue-fix scenarios (Issue #254 batch 2).

Only ``gh issue view <number> --json number,title,body,labels,assignees`` is supported, and only
when ``<number>`` matches the issue recorded in the scenario's offline fixture
(``.meta-harness/gh-issue-fixture.json``). Any other subcommand, a mismatched issue number, or a
missing/partial ``--json`` field list fails closed (non-zero exit + stderr message) instead of
silently returning the fixture payload regardless of what was actually requested (PR #266 review,
point 1) or hanging on a real network call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_FIXTURE_PATH = Path(".meta-harness/gh-issue-fixture.json")
_REQUIRED_JSON_FIELDS = frozenset({"number", "title", "body", "labels", "assignees"})


def _option(args: list[str], name: str) -> str | None:
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return None


def main() -> int:
    args = sys.argv[1:]
    if args[:2] != ["issue", "view"]:
        print(f"unsupported fake gh invocation: {args}", file=sys.stderr)
        return 2
    if len(args) < 3 or args[2].startswith("--"):
        print(f"missing issue number: {args}", file=sys.stderr)
        return 2
    requested_number = args[2]

    json_fields_raw = _option(args, "--json")
    if json_fields_raw is None:
        print(f"missing required --json flag: {args}", file=sys.stderr)
        return 2
    requested_fields = frozenset(
        field.strip() for field in json_fields_raw.split(",") if field.strip()
    )
    if requested_fields != _REQUIRED_JSON_FIELDS:
        print(
            f"unexpected --json field set {sorted(requested_fields)}, expected "
            f"{sorted(_REQUIRED_JSON_FIELDS)}: {args}",
            file=sys.stderr,
        )
        return 2

    if not _FIXTURE_PATH.is_file():
        print(f"missing gh issue fixture: {_FIXTURE_PATH}", file=sys.stderr)
        return 2
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    fixture_number = payload.get("number")
    if str(fixture_number) != str(requested_number):
        print(
            f"requested issue #{requested_number} does not match fixture issue #{fixture_number}",
            file=sys.stderr,
        )
        return 2

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
