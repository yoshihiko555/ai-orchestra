#!/usr/bin/env python3
"""Assert an issue-fix decision artifact matches the skill's real label mapping tables.

``issue-fix``'s SKILL.md derives the branch prefix, commit prefix, and PR label purely from the
GitHub issue's label (see "フォールバック: ブランチ作成" and the commit-prefix table in
``.agents/skills/issue-fix/SKILL.md``). This fixture recomputes the expected decision from the
same fixed mapping the skill documents, and diffs it against the JSON decision artifact Claude
was asked to write. This harness runs with git write operations unavailable (read-only git
snapshot, ADR-20260712-034), so scenarios ask Claude to *record* what it would do instead of
actually running ``git checkout -b`` / ``git commit`` / ``git push``.

The scenario's ``.meta-harness/gh-issue-fixture.json`` lives inside the same writable workspace
the candidate has unrestricted ``Edit``/``Write`` access to for the rest of the run, so this
fixture must not trust its *content* at face value (a candidate could rewrite it to fake a more
convenient label/number and then trivially satisfy a looser oracle). Instead, the expected issue
number/label is looked up from a hardcoded, sha256-keyed table of known-good fixture payloads;
any byte-level deviation from a known-good fixture fails closed (PR #266 review round 3, point 4).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

# label -> (branch prefix, commit prefix, pr label), per issue-fix SKILL.md's branch-prefix
# table and the PR Standards Policy's branch-prefix -> label mapping.
_LABEL_TABLE: dict[str, tuple[str, str, str]] = {
    "bug": ("fix/", "fix:", "bug"),
    "feature": ("feat/", "feat:", "enhancement"),
    "task": ("chore/", "chore:", "task"),
}
_DEFAULT_ROW: tuple[str, str, str] = ("fix/", "fix:", "bug")

# issue-fix SKILL.md: "`{slug}` は Issue タイトルから英語 kebab-case で生成（最大 30 文字）".
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_SLUG_LENGTH = 30

# sha256(exact bytes of the `.meta-harness/gh-issue-fixture.json` a scenario's `setup:` writes)
# -> the trusted (number, label) it represents. Keeping this keyed by content hash (rather than
# re-parsing the workspace copy's JSON) means the expected decision never depends on anything a
# candidate could have rewritten during the run.
_KNOWN_ISSUE_FIXTURES: dict[str, tuple[int, str]] = {
    # fix-greet-none-bug.yaml: issue #301, label "bug"
    "c29c8bc780ad05f05490816e43c8ef4ef582c2f2266b72bb12cd8ed2d0b7096d": (301, "bug"),
    # fix-formal-greeting-feature-holdout.yaml: issue #305, label "feature"
    "f3ae1d7882e84c9652622c1e01779bfc749aac6d6ff129d2aa04b14e47e3d285": (305, "feature"),
}

_EXPECTED_DECISION_KEYS = frozenset({"branch", "commit_message", "pr_label"})

# Must match `_REQUIRED_JSON_FIELDS` in fake-gh-issue-view.py (kept in sync by
# `test_gh_fixture_known_hashes_match_oracle_known_hashes` in test_skill_scenarios.py).
_EXPECTED_GH_JSON_FIELDS: tuple[str, ...] = tuple(
    sorted({"number", "title", "body", "labels", "assignees"})
)


def _assert_branch_slug(branch: str, branch_prefix: str, number: int) -> None:
    """Validate the full `{prefix}issue-{number}-{slug}` shape, not just a substring match.

    PR #266 review round 2, point 4: the original check only required `f"issue-{number}-"` to
    appear *somewhere* in the branch name, which would also accept e.g. a stray trailing dash
    with an empty slug, an uppercase/underscore slug, or an absurdly long slug. This recomputes
    the exact expected head and validates the remaining slug is non-empty, kebab-case, and within
    the skill's documented length convention.
    """
    expected_head = f"{branch_prefix}issue-{number}-"
    assert branch.startswith(expected_head), (
        f"branch {branch!r} does not follow the {expected_head!r} convention"
    )
    slug = branch[len(expected_head) :]
    assert slug, f"branch {branch!r} has an empty slug after {expected_head!r}"
    assert _SLUG_PATTERN.match(slug), (
        f"branch slug {slug!r} is not kebab-case (expected {_SLUG_PATTERN.pattern})"
    )
    assert len(slug) <= _MAX_SLUG_LENGTH, (
        f"branch slug {slug!r} exceeds the {_MAX_SLUG_LENGTH}-char convention ({len(slug)} chars)"
    )


def _trusted_issue(fixture_full: Path) -> tuple[int, str]:
    digest = hashlib.sha256(fixture_full.read_bytes()).hexdigest()
    known = _KNOWN_ISSUE_FIXTURES.get(digest)
    assert known is not None, (
        f"gh-issue-fixture.json content (sha256={digest}) does not match any known-good "
        "fixture; the writable workspace copy may have been tampered with"
    )
    return known


def assert_decision(project_root: Path, fixture_path: Path, artifact_path: Path) -> None:
    fixture_full = project_root / fixture_path
    assert fixture_full.is_file() and not fixture_full.is_symlink(), (
        f"missing regular gh issue fixture: {fixture_path}"
    )
    number, label = _trusted_issue(fixture_full)
    branch_prefix, commit_prefix, pr_label = _LABEL_TABLE.get(label, _DEFAULT_ROW)

    artifact_full = project_root / artifact_path
    assert artifact_full.is_file() and not artifact_full.is_symlink(), (
        f"missing regular decision artifact: {artifact_path}"
    )
    decision: dict[str, Any] = json.loads(artifact_full.read_text(encoding="utf-8"))

    actual_keys = frozenset(decision.keys())
    assert actual_keys == _EXPECTED_DECISION_KEYS, (
        f"decision artifact keys {sorted(actual_keys)} != expected "
        f"{sorted(_EXPECTED_DECISION_KEYS)} (no extra/missing keys allowed)"
    )

    branch = str(decision.get("branch", ""))
    commit_message = str(decision.get("commit_message", ""))
    recorded_label = str(decision.get("pr_label", ""))

    _assert_branch_slug(branch, branch_prefix, number)
    assert commit_message.startswith(commit_prefix), (
        f"commit_message {commit_message!r} does not start with {commit_prefix!r}"
    )
    assert f"Closes #{number}" in commit_message, (
        f"commit_message {commit_message!r} missing 'Closes #{number}'"
    )
    assert recorded_label == pr_label, f"pr_label {recorded_label!r} != expected {pr_label!r}"


def assert_gh_call_logged(project_root: Path, fixture_path: Path, call_log_path: Path) -> None:
    """Verify the fake `gh` stub recorded (at least one) correctly-argued invocation.

    Note (Issue #267): the call log itself lives in the same writable workspace a candidate has
    unrestricted Edit/Write access to, so a sufficiently adversarial candidate could still forge
    or delete log entries after the fact -- this check raises the bar (a legitimate run must
    leave a consistent, correctly-hashed log entry behind) but does not by itself close that gap;
    see Issue #267 for the harness-level fix (isolating the oracle/stub trust boundary from
    candidate-writable storage).
    """
    fixture_full = project_root / fixture_path
    number, _label = _trusted_issue(fixture_full)
    expected_sha256 = hashlib.sha256(fixture_full.read_bytes()).hexdigest()

    log_full = project_root / call_log_path
    assert log_full.is_file() and not log_full.is_symlink(), (
        f"missing regular gh call log: {call_log_path}"
    )
    entries = [
        json.loads(line)
        for line in log_full.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    matching = [
        entry
        for entry in entries
        if str(entry.get("requested_number")) == str(number)
        and entry.get("requested_json_fields") == list(_EXPECTED_GH_JSON_FIELDS)
        and entry.get("served_sha256") == expected_sha256
    ]
    assert matching, (
        f"no gh call log entry for issue #{number} with --json fields "
        f"{list(_EXPECTED_GH_JSON_FIELDS)} and served sha256={expected_sha256}; "
        f"entries={entries!r}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--call-log", type=Path)
    args = parser.parse_args(argv)
    if args.artifact is None and args.call_log is None:
        parser.error("either --artifact or --call-log (or both) must be provided")
    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    if args.artifact is not None:
        assert_decision(project_root, args.fixture, args.artifact)
    if args.call_log is not None:
        assert_gh_call_logged(project_root, args.fixture, args.call_log)


if __name__ == "__main__":
    main()
