#!/usr/bin/env python3
"""Assert task-state Plans.md outcomes with section-scoped, non-collateral-damage checks.

Both task-state scenarios seed a fixed ``.claude/Plans.md`` fixture (see
``scenarios/skill/task-state/*.yaml``) and ask Claude to apply exactly one edit (a task status
change, or a new Decisions entry). Plain substring checks over the whole file cannot catch Claude
silently deleting unrelated sections such as ``## Notes``, writing a decision entry into the wrong
section, using an arbitrary/stale date, dropping an unrelated task line, duplicating a task line,
or corrupting the CODD frontmatter -- this fixture independently re-parses the file structure and
enforces those invariants exactly (PR #266 review round 1 points 2/3/4/5, round 2 points 1/3/6).
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
from pathlib import Path

_TASK_LINE_PATTERN = re.compile(r"^- `cc:[A-Za-z]+` .+$", re.MULTILINE)


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


def _assert_frontmatter_preserved(plans_text: str, baseline_text: str) -> None:
    """Diff the frontmatter block against an untouched baseline copy of the same fixture.

    PR #266 review round 2, point 1: the previous checks never inspected the leading CODD
    frontmatter block at all, so deleting it entirely (or editing `node_id`/`kind`/`status`)
    still passed. Comparing against a `setup:`-written baseline copy (rather than a hardcoded
    literal) keeps this in sync with whatever the scenario's fixture actually contains.
    """
    actual = _frontmatter_block(plans_text)
    expected = _frontmatter_block(baseline_text)
    assert actual == expected, (
        f"CODD frontmatter block was modified:\nexpected={expected!r}\nactual={actual!r}"
    )


def _task_lines(text: str) -> list[str]:
    return _TASK_LINE_PATTERN.findall(text)


def _assert_task_lines_exact(text: str, expected_lines: list[str]) -> None:
    """Assert the task-marker lines in the whole document exactly match `expected_lines`.

    PR #266 review round 2, point 6: the previous checks only verified specific lines were
    *present*, so duplicating a task line or appending an extraneous stray task line still
    passed. Comparing sorted multisets catches both duplication and additions/removals.
    """
    actual_sorted = sorted(_task_lines(text))
    expected_sorted = sorted(expected_lines)
    assert actual_sorted == expected_sorted, (
        "task lines do not exactly match the expected set (duplicated or extraneous lines "
        f"fail):\nexpected={expected_sorted!r}\nactual={actual_sorted!r}"
    )


def assert_mark_task_done(
    plans_path: Path,
    *,
    baseline_path: Path,
    target_task: str,
    target_status: str,
    target_previous_status: str,
    other_tasks: list[tuple[str, str]],
    expected_decisions: list[str],
    expected_notes: list[str],
) -> None:
    text = plans_path.read_text(encoding="utf-8")

    done_line = f"`cc:{target_status}` {target_task}"
    stale_line = f"`cc:{target_previous_status}` {target_task}"
    assert done_line in text, f"expected {done_line!r} not found:\n{text}"
    assert stale_line not in text, f"stale status {stale_line!r} still present:\n{text}"

    for status, name in other_tasks:
        line = f"`cc:{status}` {name}"
        assert line in text, f"unrelated task line missing (collateral edit): {line!r}\n{text}"

    expected_task_lines = [f"- {done_line}"] + [
        f"- `cc:{status}` {name}" for status, name in other_tasks
    ]
    _assert_task_lines_exact(text, expected_task_lines)

    decisions = _section(text, "Decisions")
    for entry in expected_decisions:
        assert entry in decisions, f"Decisions section lost an entry: {entry!r}\n{decisions}"

    notes = _section(text, "Notes")
    for entry in expected_notes:
        assert entry in notes, f"Notes section lost an entry: {entry!r}\n{notes}"

    baseline_text = baseline_path.read_text(encoding="utf-8")
    _assert_frontmatter_preserved(text, baseline_text)


def assert_decision_recorded(
    plans_path: Path,
    *,
    baseline_path: Path,
    decision_substrings: list[str],
    existing_decision: str,
    other_tasks: list[tuple[str, str]],
    expected_notes: list[str],
) -> None:
    text = plans_path.read_text(encoding="utf-8")
    decisions = _section(text, "Decisions")

    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    tomorrow = today + datetime.timedelta(days=1)
    # Tolerate yesterday/today/tomorrow: the scenario run and this oracle's separate container
    # can be up to `timeout_ms` (5 min) apart, so a run started just before local midnight and
    # checked just after (or vice versa, depending on container timezone) must not flake
    # (PR #266 review round 2, point 3).
    allowed_dates = {yesterday.isoformat(), today.isoformat(), tomorrow.isoformat()}
    date_line_pattern = re.compile(r"^- (\d{4}-\d{2}-\d{2}): ")
    dated_lines = []
    for line in decisions.splitlines():
        date_match = date_line_pattern.match(line)
        if date_match and date_match.group(1) in allowed_dates:
            dated_lines.append(line)
    assert dated_lines, (
        f"no Decisions line dated within {sorted(allowed_dates)} found:\n{decisions}"
    )
    matched = [
        line for line in dated_lines if all(substring in line for substring in decision_substrings)
    ]
    assert matched, f"no dated Decisions line contains all of {decision_substrings!r}:\n{decisions}"

    assert existing_decision in decisions, (
        f"existing Decisions entry was lost: {existing_decision!r}\n{decisions}"
    )

    for status, name in other_tasks:
        line = f"`cc:{status}` {name}"
        assert line in text, f"decision-mode edit touched the task list: {line!r}\n{text}"

    expected_task_lines = [f"- `cc:{status}` {name}" for status, name in other_tasks]
    _assert_task_lines_exact(text, expected_task_lines)

    notes = _section(text, "Notes")
    for entry in expected_notes:
        assert entry in notes, f"Notes section lost an entry: {entry!r}\n{notes}"

    baseline_text = baseline_path.read_text(encoding="utf-8")
    _assert_frontmatter_preserved(text, baseline_text)


def _parse_other_task(value: str) -> tuple[str, str]:
    status, separator, name = value.partition("::")
    if not separator or not status or not name:
        raise argparse.ArgumentTypeError(f"expected STATUS::NAME, got {value!r}")
    return status, name


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)

    mark_done = subparsers.add_parser("mark-task-done")
    mark_done.add_argument("--plans", type=Path, required=True)
    mark_done.add_argument("--baseline", type=Path, required=True)
    mark_done.add_argument("--target-task", required=True)
    mark_done.add_argument("--target-status", required=True)
    mark_done.add_argument("--target-previous-status", required=True)
    mark_done.add_argument("--other-task", type=_parse_other_task, action="append", default=[])
    mark_done.add_argument("--expected-decision", action="append", default=[])
    mark_done.add_argument("--expected-note", action="append", default=[])

    record_decision = subparsers.add_parser("record-decision")
    record_decision.add_argument("--plans", type=Path, required=True)
    record_decision.add_argument("--baseline", type=Path, required=True)
    record_decision.add_argument("--decision-substring", action="append", default=[], required=True)
    record_decision.add_argument("--existing-decision", required=True)
    record_decision.add_argument(
        "--other-task", type=_parse_other_task, action="append", default=[]
    )
    record_decision.add_argument("--expected-note", action="append", default=[])

    args = parser.parse_args(argv)
    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    plans_path = project_root / args.plans
    baseline_path = project_root / args.baseline

    if args.mode == "mark-task-done":
        assert_mark_task_done(
            plans_path,
            baseline_path=baseline_path,
            target_task=args.target_task,
            target_status=args.target_status,
            target_previous_status=args.target_previous_status,
            other_tasks=args.other_task,
            expected_decisions=args.expected_decision,
            expected_notes=args.expected_note,
        )
    else:
        assert_decision_recorded(
            plans_path,
            baseline_path=baseline_path,
            decision_substrings=args.decision_substring,
            existing_decision=args.existing_decision,
            other_tasks=args.other_task,
            expected_notes=args.expected_note,
        )


if __name__ == "__main__":
    main()
