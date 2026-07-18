#!/usr/bin/env python3
"""Deterministic offline ``gh`` fixture for issue-fix scenarios (Issue #254 batch 2).

Only ``gh issue view <number> --json number,title,body,labels,assignees`` is supported, and only
when ``<number>`` matches the issue recorded in the scenario's offline fixture
(``.meta-harness/gh-issue-fixture.json``). Any other subcommand, a mismatched issue number, or a
missing/partial ``--json`` field list fails closed (non-zero exit + stderr message) instead of
silently returning the fixture payload regardless of what was actually requested (PR #266 review
round 1, point 1) or hanging on a real network call.

The argv is matched *exactly* against the 5-element ``issue view <number> --json <fields>``
template -- any extra, missing, duplicate, or reordered argument (e.g. an appended ``--repo
other/repo``, or a second ``--json``) fails closed instead of being silently ignored (PR #266
review round 5, point 2).

Before serving, the fixture's exact bytes are hashed and checked against a hardcoded known-good
set (kept in sync with ``assert-issue-fix-decision.py``'s ``_KNOWN_ISSUE_FIXTURES`` keys, both
derived from the real scenario `setup:` output and cross-checked by a dedicated regression test):
a fixture edited-then-restored between the `setup:` step and this invocation still fails closed
(PR #266 review round 3, point 3). Each successful call is appended to
``.meta-harness/gh-call-log.jsonl`` (requested issue number, requested ``--json`` fields, the
served fixture's sha256, and a timestamp) so the oracle can verify a correctly-argued call
actually happened (PR #266 review round 3, point 2). Note (Issue #267): this log lives in the
same writable workspace a candidate has unrestricted Edit/Write access to, so it cannot by itself
rule out a sufficiently adversarial candidate forging or deleting entries -- see Issue #267 for
the harness-level fix.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

_FIXTURE_PATH = Path(".meta-harness/gh-issue-fixture.json")
_CALL_LOG_PATH = Path(".meta-harness/gh-call-log.jsonl")
_REQUIRED_JSON_FIELDS = frozenset({"number", "title", "body", "labels", "assignees"})

# Must match the keys of `_KNOWN_ISSUE_FIXTURES` in assert-issue-fix-decision.py. Both are
# computed from the exact bytes the real scenario `setup:` steps write; kept in sync by
# `test_gh_fixture_known_hashes_match_oracle_known_hashes` in test_skill_scenarios.py.
_KNOWN_FIXTURE_HASHES = frozenset(
    {
        # fix-greet-none-bug.yaml: issue #301
        "c29c8bc780ad05f05490816e43c8ef4ef582c2f2266b72bb12cd8ed2d0b7096d",
        # fix-formal-greeting-feature-holdout.yaml: issue #305
        "f3ae1d7882e84c9652622c1e01779bfc749aac6d6ff129d2aa04b14e47e3d285",
    }
)


_EXPECTED_ARGV_LENGTH = 5  # "issue" "view" "<number>" "--json" "<fields>"


def _parse_argv(args: list[str]) -> tuple[str, str] | None:
    """Return ``(issue_number, json_fields_raw)`` iff `args` exactly matches the 5-element
    ``issue view <number> --json <fields>`` template; ``None`` for anything else, including
    extra/missing/duplicate/reordered arguments."""
    if len(args) != _EXPECTED_ARGV_LENGTH:
        return None
    if args[0] != "issue" or args[1] != "view" or args[3] != "--json":
        return None
    if args[2].startswith("--"):
        return None
    return args[2], args[4]


def _append_call_log(
    *, requested_number: str, requested_fields: list[str], served_sha256: str
) -> None:
    _CALL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "requested_number": requested_number,
        "requested_json_fields": requested_fields,
        "served_sha256": served_sha256,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    with _CALL_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> int:
    args = sys.argv[1:]
    if args[:2] != ["issue", "view"]:
        print(f"unsupported fake gh invocation: {args}", file=sys.stderr)
        return 2

    parsed = _parse_argv(args)
    if parsed is None:
        print(
            "argv does not exactly match 'issue view <number> --json <fields>' (extra, "
            f"missing, duplicate, or reordered arguments are not allowed): {args}",
            file=sys.stderr,
        )
        return 2
    requested_number, json_fields_raw = parsed

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
    raw = _FIXTURE_PATH.read_bytes()
    served_sha256 = hashlib.sha256(raw).hexdigest()
    if served_sha256 not in _KNOWN_FIXTURE_HASHES:
        print(
            f"gh issue fixture content (sha256={served_sha256}) does not match any known-good "
            "fixture; refusing to serve a possibly-tampered payload",
            file=sys.stderr,
        )
        return 2

    payload = json.loads(raw.decode("utf-8"))
    fixture_number = payload.get("number")
    if str(fixture_number) != str(requested_number):
        print(
            f"requested issue #{requested_number} does not match fixture issue #{fixture_number}",
            file=sys.stderr,
        )
        return 2

    _append_call_log(
        requested_number=requested_number,
        requested_fields=sorted(requested_fields),
        served_sha256=served_sha256,
    )

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
