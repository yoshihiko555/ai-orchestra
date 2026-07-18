#!/usr/bin/env python3
"""Assert an issue-fix decision artifact matches the skill's real label mapping tables.

``issue-fix``'s SKILL.md derives the branch prefix, commit prefix, and PR label purely from the
GitHub issue's label (see "フォールバック: ブランチ作成" and the commit-prefix table in
``.agents/skills/issue-fix/SKILL.md``). This fixture recomputes the expected decision from the
same fixed mapping the skill documents, using the issue label recorded in the scenario's offline
``gh issue view`` fixture -- instead of hardcoding the expected branch/commit strings inside each
scenario YAML -- and diffs it against the JSON decision artifact Claude was asked to write. This
harness runs with git write operations unavailable (read-only git snapshot, ADR-20260712-034), so
scenarios ask Claude to *record* what it would do instead of actually running
``git checkout -b`` / ``git commit`` / ``git push``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

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


def _issue_label(issue: dict) -> str:
    labels = issue.get("labels") or []
    names = {str(item.get("name")) for item in labels if isinstance(item, dict)}
    for name in ("bug", "feature", "task"):
        if name in names:
            return name
    return ""


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


def assert_decision(project_root: Path, fixture_path: Path, artifact_path: Path) -> None:
    fixture_full = project_root / fixture_path
    issue = json.loads(fixture_full.read_text(encoding="utf-8"))
    label = _issue_label(issue)
    branch_prefix, commit_prefix, pr_label = _LABEL_TABLE.get(label, _DEFAULT_ROW)
    number = issue["number"]

    artifact_full = project_root / artifact_path
    assert artifact_full.is_file() and not artifact_full.is_symlink(), (
        f"missing regular decision artifact: {artifact_path}"
    )
    decision = json.loads(artifact_full.read_text(encoding="utf-8"))

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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args(argv)
    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    assert_decision(project_root, args.fixture, args.artifact)


if __name__ == "__main__":
    main()
