#!/usr/bin/env python3
"""Assert task-state Plans.md outcomes with section-scoped, non-collateral-damage checks.

Both task-state scenarios seed a fixed ``.claude/Plans.md`` fixture (see
``scenarios/skill/task-state/*.yaml``) and ask Claude to apply exactly one edit (a task status
change, or a new Decisions entry). Plain substring checks over the whole file cannot catch Claude
silently deleting unrelated sections such as ``## Notes``, writing a decision entry into the wrong
section, using an arbitrary/stale date, dropping/duplicating/reordering a task line, or corrupting
the CODD frontmatter -- this fixture independently re-parses the file structure and enforces those
invariants exactly (PR #266 review round 1 points 2/3/4/5, round 2 points 1/3/6, round 3 points
3/5/6).

Everything the candidate could have rewritten during the run -- including a prior design's
``.meta-harness/plans-baseline.md`` "baseline copy" -- lives inside the same writable workspace
the candidate has unrestricted ``Edit``/``Write`` access to, so none of it can serve as ground
truth for this oracle (PR #266 review round 3, point 6). The expected fixture content is instead
embedded as a literal constant below, matching exactly what every task-state scenario's `setup:`
step writes.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
from pathlib import Path

_TASK_LINE_PATTERN = re.compile(r"^- `cc:[A-Za-z]+` .+$", re.MULTILINE)

# The exact `.claude/Plans.md` content every task-state scenario's `setup:` step writes (see
# `scenarios/skill/task-state/*.yaml`). This is the only trusted "before" state: it is a literal
# constant in this script, not read from the writable workspace.
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


def _section(text: str, heading: str) -> str:
    """Return the body of a top-level ``## <heading>`` section (exclusive of the next ``## ``)."""
    match = re.search(rf"(?m)^## {re.escape(heading)}\s*$", text)
    if not match:
        raise AssertionError(f"missing '## {heading}' section")
    start = match.end()
    next_heading = re.search(r"(?m)^## ", text[start:])
    end = start + next_heading.start() if next_heading else len(text)
    return text[start:end]


def _frontmatter_block(text: str) -> str:
    """Return the leading ``---\\n...\\n---\\n`` CODD frontmatter block, verbatim."""
    match = re.match(r"\A---\n.*?\n---\n", text, re.DOTALL)
    if not match:
        raise AssertionError("missing leading '---\\ncodd:...\\n---' frontmatter block")
    return match.group(0)


def _bullet_lines(section_text: str) -> list[str]:
    return [line for line in section_text.splitlines() if line.startswith("- ")]


_CANONICAL_FRONTMATTER = _frontmatter_block(_CANONICAL_PLANS_FIXTURE)
_CANONICAL_DECISIONS_BULLETS = _bullet_lines(_section(_CANONICAL_PLANS_FIXTURE, "Decisions"))
_CANONICAL_NOTES_BULLETS = _bullet_lines(_section(_CANONICAL_PLANS_FIXTURE, "Notes"))


def _assert_frontmatter_preserved(text: str) -> None:
    """Diff the frontmatter block against the hardcoded canonical fixture (never the workspace)."""
    actual = _frontmatter_block(text)
    assert actual == _CANONICAL_FRONTMATTER, (
        f"CODD frontmatter block was modified:\nexpected={_CANONICAL_FRONTMATTER!r}\n"
        f"actual={actual!r}"
    )


def _task_lines(text: str) -> list[str]:
    return _TASK_LINE_PATTERN.findall(text)


def _assert_task_lines_exact_ordered(text: str, expected_tasks: list[tuple[str, str]]) -> None:
    """Assert the task-marker lines exactly match `expected_tasks`, in the same order.

    PR #266 review round 2, point 6 (duplication/extraneous lines) and round 3, point 3
    (reordering must also fail): an order-sensitive list comparison catches all three at once.
    """
    expected_lines = [f"- `cc:{status}` {name}" for status, name in expected_tasks]
    actual_lines = _task_lines(text)
    assert actual_lines == expected_lines, (
        "task lines do not exactly match the expected ordered sequence (reordering, "
        f"duplication, or extraneous lines fail):\nexpected={expected_lines!r}\n"
        f"actual={actual_lines!r}"
    )


def assert_mark_task_done(
    plans_path: Path,
    *,
    expected_tasks: list[tuple[str, str]],
) -> None:
    text = plans_path.read_text(encoding="utf-8")

    _assert_task_lines_exact_ordered(text, expected_tasks)

    # PR #266 review round 3, point 5: Decisions/Notes must be byte-identical to the seeded
    # fixture in this mode (no entry may be added, removed, or reordered).
    decisions_bullets = _bullet_lines(_section(text, "Decisions"))
    assert decisions_bullets == _CANONICAL_DECISIONS_BULLETS, (
        f"Decisions section changed (must stay identical to the seeded fixture):\n"
        f"expected={_CANONICAL_DECISIONS_BULLETS!r}\nactual={decisions_bullets!r}"
    )

    notes_bullets = _bullet_lines(_section(text, "Notes"))
    assert notes_bullets == _CANONICAL_NOTES_BULLETS, (
        f"Notes section changed (must stay identical to the seeded fixture):\n"
        f"expected={_CANONICAL_NOTES_BULLETS!r}\nactual={notes_bullets!r}"
    )

    _assert_frontmatter_preserved(text)


def assert_decision_recorded(
    plans_path: Path,
    *,
    decision_substrings: list[str],
    expected_tasks: list[tuple[str, str]],
) -> None:
    text = plans_path.read_text(encoding="utf-8")

    _assert_task_lines_exact_ordered(text, expected_tasks)

    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    tomorrow = today + datetime.timedelta(days=1)
    # Tolerate yesterday/today/tomorrow: the scenario run and this oracle's separate container
    # can be up to `timeout_ms` (5 min) apart, so a run started just before local midnight and
    # checked just after (or vice versa, depending on container timezone) must not flake
    # (PR #266 review round 2, point 3).
    allowed_dates = {yesterday.isoformat(), today.isoformat(), tomorrow.isoformat()}
    date_line_pattern = re.compile(r"^- (\d{4}-\d{2}-\d{2}): ")

    # PR #266 review round 3, point 5: the Decisions section must contain exactly the seeded
    # entries, unmodified and in their original order, immediately followed by exactly one new
    # entry -- not merely "the old entry is still present somewhere".
    decisions_bullets = _bullet_lines(_section(text, "Decisions"))
    assert len(decisions_bullets) == len(_CANONICAL_DECISIONS_BULLETS) + 1, (
        "Decisions section must contain exactly the seeded entries plus exactly one new "
        f"entry:\nexpected {len(_CANONICAL_DECISIONS_BULLETS)} seeded + 1 new, "
        f"got {len(decisions_bullets)}: {decisions_bullets!r}"
    )
    seeded_prefix = decisions_bullets[: len(_CANONICAL_DECISIONS_BULLETS)]
    assert seeded_prefix == _CANONICAL_DECISIONS_BULLETS, (
        f"existing Decisions entries were modified or reordered:\nexpected="
        f"{_CANONICAL_DECISIONS_BULLETS!r}\nactual={seeded_prefix!r}"
    )
    new_line = decisions_bullets[-1]
    date_match = date_line_pattern.match(new_line)
    assert date_match and date_match.group(1) in allowed_dates, (
        f"new Decisions entry is not dated within {sorted(allowed_dates)}: {new_line!r}"
    )
    assert all(substring in new_line for substring in decision_substrings), (
        f"new Decisions entry does not contain all of {decision_substrings!r}: {new_line!r}"
    )

    notes_bullets = _bullet_lines(_section(text, "Notes"))
    assert notes_bullets == _CANONICAL_NOTES_BULLETS, (
        f"Notes section changed (must stay identical to the seeded fixture):\n"
        f"expected={_CANONICAL_NOTES_BULLETS!r}\nactual={notes_bullets!r}"
    )

    _assert_frontmatter_preserved(text)


def _parse_task(value: str) -> tuple[str, str]:
    status, separator, name = value.partition("::")
    if not separator or not status or not name:
        raise argparse.ArgumentTypeError(f"expected STATUS::NAME, got {value!r}")
    return status, name


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)

    mark_done = subparsers.add_parser("mark-task-done")
    mark_done.add_argument("--plans", type=Path, required=True)
    mark_done.add_argument(
        "--expected-task", type=_parse_task, action="append", default=[], required=True
    )

    record_decision = subparsers.add_parser("record-decision")
    record_decision.add_argument("--plans", type=Path, required=True)
    record_decision.add_argument("--decision-substring", action="append", default=[], required=True)
    record_decision.add_argument(
        "--expected-task", type=_parse_task, action="append", default=[], required=True
    )

    args = parser.parse_args(argv)
    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    plans_path = project_root / args.plans

    if args.mode == "mark-task-done":
        assert_mark_task_done(plans_path, expected_tasks=args.expected_task)
    else:
        assert_decision_recorded(
            plans_path,
            decision_substrings=args.decision_substring,
            expected_tasks=args.expected_task,
        )


if __name__ == "__main__":
    main()
