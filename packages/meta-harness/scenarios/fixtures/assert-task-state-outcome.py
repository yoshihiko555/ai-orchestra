#!/usr/bin/env python3
"""Assert task-state Plans.md outcomes by diffing the whole document against the canonical fixture.

Both task-state scenarios seed a fixed ``.claude/Plans.md`` fixture (see
``scenarios/skill/task-state/*.yaml``) and ask Claude to apply exactly one edit (a task status
change, or a new Decisions entry). Rather than checking sections/lines piecemeal (which could
miss e.g. a deleted heading or an extra blank line falling outside any single checked region),
this fixture compares the *entire* document, line by line, against a hardcoded canonical
constant and asserts the diff is exactly the one expected edit -- nothing else may differ
(PR #266 review round 4, point 1; supersedes the section-scoped checks from round 1 points 2-5,
round 2 points 1/3/6, round 3 points 3/5/6, which are now subsumed by this whole-document diff).

Everything the candidate could have rewritten during the run -- including a prior design's
``.meta-harness/plans-baseline.md`` "baseline copy" -- lives inside the same writable workspace
the candidate has unrestricted ``Edit``/``Write`` access to, so none of it can serve as ground
truth for this oracle (PR #266 review round 3, point 6). The expected fixture content is instead
embedded as a literal constant below, matching exactly what every task-state scenario's `setup:`
step writes.

The new Decisions line in ``record-decision`` mode is matched *exactly* (date prefix aside), not
by substring containment: a substring check would also accept a negated or otherwise materially
different decision text as long as it happened to contain the same keywords (PR #266 review
round 5, point 1).
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
from pathlib import Path

# The exact `.claude/Plans.md` content every task-state scenario's `setup:` step writes (see
# `scenarios/skill/task-state/*.yaml`). This is the only trusted "before" state: it is a literal
# constant in this script, never read from the writable workspace.
_CANONICAL_PLANS_FIXTURE = (
    "---\n"
    "codd:\n"
    '  node_id: "plan:meta-harness-eval-fixture"\n'
    "  kind: plan\n"
    "  status: active\n"
    "  depends_on:\n"
    '    - id: "design:meta-harness"\n'
    "      relation: implements\n"
    "---\n"
    "\n"
    "# Plans\n"
    "\n"
    "## Project: meta-harness-eval-fixture\n"
    "\n"
    "### Phase 2: 実装 `cc:WIP`\n"
    "\n"
    "#### API\n"
    "\n"
    "- `cc:done` ユーザー認証API\n"
    "- `cc:WIP` 商品一覧API\n"
    "- `cc:TODO` 注文API\n"
    "\n"
    "---\n"
    "\n"
    "## Decisions\n"
    "\n"
    "- 2026-01-01: 初期設計方針を確定\n"
    "\n"
    "## Notes\n"
    "\n"
    "- 評価用フィクスチャ\n"
)

_CANONICAL_LINES = _CANONICAL_PLANS_FIXTURE.split("\n")

_TASK_LINE_PATTERN = re.compile(r"^- `cc:[A-Za-z]+` (.+)$")
_DECISION_LINE_PATTERN = re.compile(r"^- (\d{4}-\d{2}-\d{2}): ")

# Day-boundary tolerance: the scenario run and this oracle's separate container can be up to
# `timeout_ms` (5 min) apart, so a run started just before local midnight and checked just after
# (or vice versa, depending on container timezone) must not flake (PR #266 review round 2, point
# 3).
_DATE_TOLERANCE_DAYS = 1


def _find_unique_line_index(lines: list[str], predicate) -> int:
    matches = [idx for idx, line in enumerate(lines) if predicate(line)]
    assert len(matches) == 1, f"expected exactly one matching canonical line, found {len(matches)}"
    return matches[0]


def assert_mark_task_done(plans_path: Path, *, target_task: str, target_status: str) -> None:
    """Assert the whole document is identical to the canonical fixture except that the target
    task's marker line changed to `target_status` -- no other line may differ (added, removed,
    reordered, or edited), which inherently covers frontmatter/headings/Decisions/Notes/every
    other task without needing separate section-scoped checks."""
    text = plans_path.read_text(encoding="utf-8")
    actual_lines = text.split("\n")

    assert len(actual_lines) == len(_CANONICAL_LINES), (
        "Plans.md must have exactly the same line count as the seeded fixture (mark-task-done "
        f"only ever changes one line's content): expected {len(_CANONICAL_LINES)} lines, "
        f"got {len(actual_lines)}"
    )

    target_index = _find_unique_line_index(
        _CANONICAL_LINES,
        lambda line: (
            (match := _TASK_LINE_PATTERN.match(line)) is not None and match.group(1) == target_task
        ),
    )
    expected_line = f"- `cc:{target_status}` {target_task}"

    diff_indices = [
        idx for idx in range(len(_CANONICAL_LINES)) if actual_lines[idx] != _CANONICAL_LINES[idx]
    ]
    assert diff_indices == [target_index], (
        "Plans.md must differ from the seeded fixture at exactly the target task's line and "
        f"nowhere else: expected the only diff at line {target_index} "
        f"({_CANONICAL_LINES[target_index]!r}), got diffs at {diff_indices!r} "
        f"(actual content there: {[actual_lines[i] for i in diff_indices]!r})"
    )
    assert actual_lines[target_index] == expected_line, (
        f"target task line is {actual_lines[target_index]!r}, expected {expected_line!r}"
    )


def assert_decision_recorded(plans_path: Path, *, expected_decision: str) -> None:
    """Assert the whole document is identical to the canonical fixture except for exactly one
    new line inserted right after the last seeded Decisions bullet -- no other line may differ."""
    text = plans_path.read_text(encoding="utf-8")
    actual_lines = text.split("\n")

    assert len(actual_lines) == len(_CANONICAL_LINES) + 1, (
        "Plans.md must differ from the seeded fixture by exactly one inserted line (a new "
        f"Decisions entry): expected {len(_CANONICAL_LINES) + 1} lines, got {len(actual_lines)}"
    )

    last_decision_index = _find_unique_line_index(
        _CANONICAL_LINES,
        lambda line: _DECISION_LINE_PATTERN.match(line) is not None,
    )
    insert_at = last_decision_index + 1

    before_expected = _CANONICAL_LINES[:insert_at]
    after_expected = _CANONICAL_LINES[insert_at:]
    actual_before = actual_lines[:insert_at]
    new_line = actual_lines[insert_at]
    actual_after = actual_lines[insert_at + 1 :]

    assert actual_before == before_expected, (
        "Plans.md content before the new Decisions entry must be byte-identical to the seeded "
        f"fixture:\nexpected={before_expected!r}\nactual={actual_before!r}"
    )
    assert actual_after == after_expected, (
        "Plans.md content after the new Decisions entry must be byte-identical to the seeded "
        f"fixture:\nexpected={after_expected!r}\nactual={actual_after!r}"
    )

    today = datetime.date.today()
    allowed_dates = {
        (today + datetime.timedelta(days=offset)).isoformat()
        for offset in range(-_DATE_TOLERANCE_DAYS, _DATE_TOLERANCE_DAYS + 1)
    }
    date_match = _DECISION_LINE_PATTERN.match(new_line)
    assert date_match and date_match.group(1) in allowed_dates, (
        f"new Decisions entry is not dated within {sorted(allowed_dates)}: {new_line!r}"
    )
    # Exact match (not substring containment): a substring check would also accept a negated or
    # otherwise materially different decision text (e.g. "GraphQL は採用しない（理由: ...）")
    # as long as it happened to contain the same keywords (PR #266 review round 5, point 1).
    expected_line = f"- {date_match.group(1)}: {expected_decision}"
    assert new_line == expected_line, (
        "new Decisions entry does not exactly match the expected decision text:\n"
        f"expected={expected_line!r}\nactual={new_line!r}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)

    mark_done = subparsers.add_parser("mark-task-done")
    mark_done.add_argument("--plans", type=Path, required=True)
    mark_done.add_argument("--target-task", required=True)
    mark_done.add_argument("--target-status", required=True)

    record_decision = subparsers.add_parser("record-decision")
    record_decision.add_argument("--plans", type=Path, required=True)
    record_decision.add_argument("--expected-decision", required=True)

    args = parser.parse_args(argv)
    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    plans_path = project_root / args.plans

    if args.mode == "mark-task-done":
        assert_mark_task_done(
            plans_path, target_task=args.target_task, target_status=args.target_status
        )
    else:
        assert_decision_recorded(plans_path, expected_decision=args.expected_decision)


if __name__ == "__main__":
    main()
