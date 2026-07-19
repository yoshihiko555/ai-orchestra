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
import subprocess
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


def _run_git_z(args: list[str], *, cwd: Path | None) -> list[str]:
    """Run a NUL-terminated (`-z`) git subcommand and split its output on NUL bytes.

    PR #273 bot review round 3: the previous newline-based parsing (`splitlines()` +
    `line[3:]`) breaks on paths containing spaces or other characters git would otherwise
    quote. `-z` disables path quoting/escaping entirely and terminates each record with NUL
    instead, which is unambiguous for arbitrary filenames (it is not valid inside a POSIX
    path). The trailing empty string after the final NUL separator is dropped.
    """
    result = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
    tokens = result.stdout.split("\0")
    if tokens and tokens[-1] == "":
        tokens = tokens[:-1]
    return tokens


def _parse_diff_name_status(name_status_z_tokens: list[str]) -> list[tuple[str, str]]:
    """Parse `git diff --name-status -z` tokens into `(status, path)` pairs.

    PR #273 bot review round 4 (Codex P2): a status-blind `--name-only` diff could not tell a
    brand-new *staged* file (`git add`-ed by the candidate, status `A`) apart from a change to
    an already-tracked file (`M`/`D`/...), so `allowed_new_prefixes` never applied to staged
    additions and they always required exact membership in `allowed_paths` -- a false positive
    for e.g. a candidate that `git add`s a new file under an allowed prefix without committing.

    Non-rename entries are 2-token records: `STATUS`, `PATH`. Rename/copy entries (status
    `R###`/`C###`) are 3-token records: `STATUS`, `OLD_PATH`, `NEW_PATH` (verified empirically:
    `git diff --name-status -z` orders the vacated path before the resulting path, matching the
    non-`-z` `STATUS\told\tnew` convention). Both the old and new paths of a rename/copy are
    yielded under the *rename's* status (never treated as a fresh `A` addition), since a
    rename/copy of a tracked file is not a new file appearing -- it is an existing tracked
    file moving, which `allowed_new_prefixes` must not excuse.
    """
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(name_status_z_tokens):
        status = name_status_z_tokens[index]
        if not status:
            index += 1
            continue
        if status[0] in "RC":
            if index + 2 >= len(name_status_z_tokens):
                break
            old_path, new_path = (
                name_status_z_tokens[index + 1],
                name_status_z_tokens[index + 2],
            )
            entries.append((status, old_path))
            entries.append((status, new_path))
            index += 3
        else:
            if index + 1 >= len(name_status_z_tokens):
                break
            entries.append((status, name_status_z_tokens[index + 1]))
            index += 2
    return entries


def _parse_untracked_paths(porcelain_z_tokens: list[str]) -> list[str]:
    """Extract `??` (untracked) paths from `git status --porcelain -z` tokens.

    Rename/copy entries (`status[0]` or `status[1]` in `RC`) consume *two* consecutive NUL
    tokens -- the new path, then the old path -- with no `XY ` prefix on the second one. We
    must skip that second token rather than misinterpret it as its own entry (its first two
    characters are arbitrary path bytes, not a status code), even though untracked entries
    themselves (`??`) can never be renames/copies.
    """
    untracked: list[str] = []
    index = 0
    while index < len(porcelain_z_tokens):
        entry = porcelain_z_tokens[index]
        status, path = entry[:2], entry[3:]
        if status == "??":
            untracked.append(path)
        if status[0] in "RC" or status[1] in "RC":
            index += 2
        else:
            index += 1
    return untracked


def assert_tracked_changes_limited_to(
    allowed_paths: set[str],
    *,
    allowed_new_prefixes: tuple[str, ...] = (),
    cwd: Path | None = None,
) -> None:
    """Assert no tracked file outside `allowed_paths` differs from the pre-run baseline commit,
    and that every brand-new *untracked* file lives under `allowed_paths` or one of
    `allowed_new_prefixes`.

    Issue #261 PR6 bot review follow-up: this is a generic collateral-damage guard for any
    scenario that must run under `permission_mode: bypassPermissions` (required because
    `.claude/` is a Claude Code protected path that allow-rule permissions cannot unlock; see
    task-state's `mark-task-done.yaml` / `record-architecture-decision-holdout.yaml`, and
    handoff's `create-handoff.yaml` / `create-handoff-holdout.yaml`, whose comments explain why
    each needs bypass). Despite the filename, this function/subcommand is not task-state
    specific -- it is deliberately reused by the handoff suite rather than duplicated.

    Tracked-file check: `git diff --name-status -z HEAD` reports staged and unstaged changes
    against the isolated git snapshot mounted for oracle containers (see
    `scenario_docker_profile.build_oracle_command`). A path is allowed if it is in
    `allowed_paths`, *or* (PR #273 bot review round 4) it is a brand-new staged addition
    (status `A`, i.e. the candidate ran `git add` on a file that did not exist in the
    baseline) whose path starts with `allowed_new_prefixes`. Modifications, deletions, and
    renames/copies of already-tracked files (`M`/`D`/`R###`/`C###`/...) are never excused by
    `allowed_new_prefixes` -- only `allowed_paths` membership permits those, since they touch
    files that already existed at baseline rather than introducing a new one.

    Untracked-file check (PR #273 bot review round 3: now unconditional, not opt-in --
    previously an early return skipped this entirely when `allowed_new_prefixes` was empty,
    which let a bypass-mode candidate create e.g. `.claude/settings.local.json` undetected).
    `git status --porcelain -z --untracked-files=all` lists brand-new files that were never
    tracked at all. Every such path must start with `allowed_new_prefixes` -- or, when a
    scenario's expected target itself may appear as untracked for reasons specific to that
    scenario's repo `.gitignore` state, be exactly one of `allowed_paths` -- or the run fails.
    Genuinely expected harness/hook side effects (`.claude/context/...` session state,
    `__pycache__`, etc.) are excluded from the repo's `.gitignore` and therefore never show up
    as untracked at all (verified against `.gitignore` for the task-state and handoff suites;
    see the calling scenario yaml's comment), so this default-deny does not flake on them.

    `cwd` defaults to the process's own working directory (the production oracle container
    sets `--workdir /workspace` and relies on the process cwd); tests pass an explicit
    temporary git repo instead.
    """
    diff_tokens = _run_git_z(["git", "diff", "--name-status", "-z", "HEAD"], cwd=cwd)
    diff_entries = _parse_diff_name_status(diff_tokens)
    unexpected = sorted(
        {
            path
            for status, path in diff_entries
            if path
            and path not in allowed_paths
            and not (status == "A" and path.startswith(allowed_new_prefixes))
        }
    )
    assert not unexpected, (
        f"tracked files changed outside the allowed scope {sorted(allowed_paths)}: {unexpected}"
    )

    status_tokens = _run_git_z(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=cwd
    )
    untracked = _parse_untracked_paths(status_tokens)
    disallowed_new = [
        path
        for path in untracked
        if path not in allowed_paths and not path.startswith(allowed_new_prefixes)
    ]
    assert not disallowed_new, (
        f"new untracked files outside the allowed scope ({sorted(allowed_paths)}) and allowed "
        f"prefixes {sorted(allowed_new_prefixes)}: {sorted(disallowed_new)}"
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

    collateral_scope = subparsers.add_parser("collateral-scope")
    collateral_scope.add_argument(
        "--allow",
        action="append",
        required=True,
        help="tracked path allowed to differ from baseline (repeatable)",
    )
    collateral_scope.add_argument(
        "--allow-new-prefix",
        action="append",
        default=[],
        help=(
            "prefix new untracked files are allowed to appear under (repeatable); "
            "if omitted, untracked files are ignored entirely"
        ),
    )

    args = parser.parse_args(argv)

    if args.mode == "collateral-scope":
        assert_tracked_changes_limited_to(
            set(args.allow), allowed_new_prefixes=tuple(args.allow_new_prefix)
        )
        return

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
