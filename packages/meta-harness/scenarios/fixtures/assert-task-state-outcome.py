#!/usr/bin/env python3
"""Assert task-state Plans.md outcomes by diffing the whole document against the canonical fixture.

All four task-state scenarios seed a fixed ``.claude/Plans.md`` fixture (see
``scenarios/skill/task-state/*.yaml``) and ask Claude to apply exactly one edit (a task status
change, a new Decisions entry, or a new phase inserted before the existing project separator).
Rather than checking sections/lines piecemeal (which could miss e.g. a deleted heading or an
extra blank line falling outside any single checked region), this fixture compares the *entire*
document, line by line, against a hardcoded canonical constant and asserts the diff is exactly
the one expected edit -- nothing else may differ (PR #266 review round 4, point 1; supersedes the
section-scoped checks from round 1 points 2-5, round 2 points 1/3/6, round 3 points 3/5/6, which
are now subsumed by this whole-document diff).

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

The ``add-phase-with-ac`` / ``add-phase-no-ac`` modes (Issue #297 / PR #326 review round 1)
lock down the `add-phase` behavior contract documented in `facets/instructions/task-state.md`:
a new phase inserted right before the existing separator must carry exactly the given
Acceptance Criteria items (verify/judge, both unchecked) when they were already agreed upon, and
must carry *no* Acceptance Criteria section at all -- rather than a fabricated one -- when the
call did not come with agreed criteria.
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

# `add-phase` heading / Acceptance Criteria patterns (Issue #297). The heading capture excludes
# the trailing `` `cc:<marker>` `` status marker so callers can compare just the phase title, and
# the marker itself is captured separately so callers can assert it is freshly-added `TODO`.
_PHASE_HEADING_PATTERN = re.compile(r"^### (.+) `cc:([A-Za-z]+)`$")
# Matches `task-memory-usage.md`'s AC heading exactly (same constant `ac_parser.AC_SECTION_HEADING`
# in `packages/core/hooks/ac_parser.py` uses, duplicated here rather than imported since this
# fixture has no dependency on the `packages/core/hooks` sys.path setup).
_AC_SECTION_HEADING = "#### Acceptance Criteria"
_AC_ITEM_VERIFY_PATTERN = re.compile(r"^- \[ \] (.+) — verify: `(.+)`$")
_AC_ITEM_JUDGE_PATTERN = re.compile(r"^- \[ \] (.+) — judge: (.+)$")
# Any `####` sub-heading other than the AC heading itself is a task-group heading (e.g.
# `#### API`); used to assert the AC section precedes the task group, not merely the phase
# heading (PR #326 review round 2: an AC section placed *after* `#### Tasks` and its task lines
# previously still passed since only `ac_heading_index > heading_index` was checked).
_SUBHEADING_PATTERN = re.compile(r"^#### (.+)$")

# Day-boundary tolerance: the scenario run and this oracle's separate container can be up to
# `timeout_ms` (5 min) apart, so a run started just before local midnight and checked just after
# (or vice versa, depending on container timezone) must not flake (PR #266 review round 2, point
# 3).
_DATE_TOLERANCE_DAYS = 1


def _find_unique_line_index(
    lines: list[str], predicate, *, not_found_message: str | None = None
) -> int:
    """Find the index of the single line matching `predicate`.

    `not_found_message`, when given, replaces the generic assertion message for the
    zero-matches case only (ambiguous multi-match still uses the generic message), so callers
    can surface a specific diagnosis instead of a vague "found 0" (Issue #297 review round 2)."""
    matches = [idx for idx, line in enumerate(lines) if predicate(line)]
    if not matches and not_found_message is not None:
        raise AssertionError(not_found_message)
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


def _extract_inserted_phase_block(plans_path: Path) -> list[str]:
    """Return the lines `add-phase` inserted, asserting everything else in the document --
    earlier phases, the project separator, Decisions, Notes, and the CODD frontmatter -- stayed
    byte-identical to the canonical fixture.

    `add-phase` only ever inserts a brand-new phase block right before the existing project
    separator (`---` immediately preceding `## Decisions`; see the worked example in
    `facets/instructions/task-state.md`); it never edits or removes any existing line. This
    mirrors `assert_mark_task_done` / `assert_decision_recorded`'s whole-document-diff approach,
    generalized from a fixed-size edit to an open-ended insertion: everything strictly before the
    separator's canonical index must match the canonical prefix, and everything from that same
    separator onward (by canonical suffix length, counted from the end of the actual document)
    must match the canonical suffix.

    The canonical fixture's frontmatter also opens and closes with a bare `---` line, so the
    project separator cannot be located by `line == "---"` alone (that matches 3 lines, not 1) --
    it is instead identified as the `---` line immediately preceding the unique `## Decisions`
    line.
    """
    text = plans_path.read_text(encoding="utf-8")
    actual_lines = text.split("\n")

    decisions_heading_index = _find_unique_line_index(
        _CANONICAL_LINES, lambda line: line == "## Decisions"
    )
    separator_index = next(
        idx for idx in range(decisions_heading_index - 1, -1, -1) if _CANONICAL_LINES[idx] == "---"
    )
    prefix_expected = _CANONICAL_LINES[:separator_index]
    suffix_expected = _CANONICAL_LINES[separator_index:]

    assert len(actual_lines) >= len(_CANONICAL_LINES), (
        "add-phase must only ever insert new lines, never remove any: expected at least "
        f"{len(_CANONICAL_LINES)} lines, got {len(actual_lines)}"
    )

    prefix_actual = actual_lines[: len(prefix_expected)]
    assert prefix_actual == prefix_expected, (
        "content before the new phase's insertion point must be byte-identical to the seeded "
        f"fixture (earlier phases must not be touched):\nexpected={prefix_expected!r}\n"
        f"actual={prefix_actual!r}"
    )

    suffix_actual = actual_lines[len(actual_lines) - len(suffix_expected) :]
    assert suffix_actual == suffix_expected, (
        "content from the project separator onward (separator, Decisions, Notes) must be "
        f"byte-identical to the seeded fixture:\nexpected={suffix_expected!r}\n"
        f"actual={suffix_actual!r}"
    )

    return actual_lines[len(prefix_expected) : len(actual_lines) - len(suffix_expected)]


def _assert_new_phase_heading_and_tasks(
    inserted_block: list[str], *, phase_name: str, tasks: list[str]
) -> int:
    """Assert `inserted_block` starts with a fresh `cc:TODO` heading for `phase_name` and ends
    with exactly `tasks` (in order, all `cc:TODO`), returning the heading's index within
    `inserted_block` so callers can locate an Acceptance Criteria section relative to it."""
    heading_index = _find_unique_line_index(
        inserted_block,
        lambda line: (
            (match := _PHASE_HEADING_PATTERN.match(line)) is not None
            and match.group(1) == phase_name
        ),
    )
    heading_match = _PHASE_HEADING_PATTERN.match(inserted_block[heading_index])
    assert heading_match is not None and heading_match.group(2) == "TODO", (
        f"a newly added phase must start as `cc:TODO`, got: {inserted_block[heading_index]!r}"
    )

    task_matches = [
        match for line in inserted_block if (match := _TASK_LINE_PATTERN.match(line)) is not None
    ]
    actual_tasks = [match.group(1) for match in task_matches]
    assert actual_tasks == tasks, (
        f"new phase tasks do not match: expected {tasks!r}, got {actual_tasks!r}"
    )
    assert all(match.group(0).startswith("- `cc:TODO`") for match in task_matches), (
        "all tasks in a newly added phase must start as `cc:TODO`, got: "
        f"{[match.group(0) for match in task_matches]!r}"
    )
    return heading_index


def assert_add_phase_with_ac(
    plans_path: Path,
    *,
    phase_name: str,
    tasks: list[str],
    verify_text: str,
    verify_command: str,
    judge_text: str,
    judge_criteria: str,
) -> None:
    """Assert a new phase was inserted with exactly one unchecked `verify` and one unchecked
    `judge` Acceptance Criteria item (matching the given text/command/criteria) placed between
    the phase heading and its tasks, and that the rest of the document is untouched (see
    `_extract_inserted_phase_block`)."""
    inserted_block = _extract_inserted_phase_block(plans_path)
    heading_index = _assert_new_phase_heading_and_tasks(
        inserted_block, phase_name=phase_name, tasks=tasks
    )

    ac_heading_index = _find_unique_line_index(
        inserted_block,
        lambda line: line == _AC_SECTION_HEADING,
        not_found_message=(
            "Acceptance Criteria セクションが欠落している: expected an "
            f"{_AC_SECTION_HEADING!r} heading in the newly inserted phase block, but none was "
            f"found: {inserted_block!r}"
        ),
    )
    assert ac_heading_index > heading_index, (
        "the Acceptance Criteria section must come after the phase heading: heading at "
        f"{heading_index}, Acceptance Criteria heading at {ac_heading_index}"
    )

    task_group_marker_indices = [
        idx
        for idx, line in enumerate(inserted_block)
        if _TASK_LINE_PATTERN.match(line) is not None
        or (_SUBHEADING_PATTERN.match(line) is not None and line != _AC_SECTION_HEADING)
    ]
    assert task_group_marker_indices, (
        "expected at least one task-group heading or task line in the newly inserted phase "
        f"block: {inserted_block!r}"
    )
    first_task_group_index = min(task_group_marker_indices)
    assert ac_heading_index < first_task_group_index, (
        "the Acceptance Criteria section must come before the task group "
        "(task-memory-usage.md: AC is placed 'タスクグループより前'): Acceptance Criteria "
        f"heading at {ac_heading_index}, first task-group marker at {first_task_group_index}"
    )

    verify_matches = [
        match
        for line in inserted_block
        if (match := _AC_ITEM_VERIFY_PATTERN.match(line)) is not None
    ]
    judge_matches = [
        match
        for line in inserted_block
        if (match := _AC_ITEM_JUDGE_PATTERN.match(line)) is not None
    ]
    assert len(verify_matches) == 1, (
        f"expected exactly one unchecked verify Acceptance Criteria item, found "
        f"{len(verify_matches)}: {[m.group(0) for m in verify_matches]!r}"
    )
    assert len(judge_matches) == 1, (
        f"expected exactly one unchecked judge Acceptance Criteria item, found "
        f"{len(judge_matches)}: {[m.group(0) for m in judge_matches]!r}"
    )
    assert verify_matches[0].groups() == (verify_text, verify_command), (
        f"verify Acceptance Criteria item does not match: expected "
        f"{(verify_text, verify_command)!r}, got {verify_matches[0].groups()!r}"
    )
    assert judge_matches[0].groups() == (judge_text, judge_criteria), (
        f"judge Acceptance Criteria item does not match: expected "
        f"{(judge_text, judge_criteria)!r}, got {judge_matches[0].groups()!r}"
    )


def assert_add_phase_no_ac(plans_path: Path, *, phase_name: str, tasks: list[str]) -> None:
    """Assert a new phase (with exactly `tasks`) was inserted *without* any Acceptance Criteria
    section -- neither the `#### Acceptance Criteria` heading nor any unchecked verify/judge item
    may appear anywhere in the inserted block. This locks the `add-phase` behavior contract for a
    direct (non-`/preflight`) call with no agreed-upon Acceptance Criteria: the skill must not
    silently fabricate one (Issue #297 / PR #326 review round 1)."""
    inserted_block = _extract_inserted_phase_block(plans_path)
    _assert_new_phase_heading_and_tasks(inserted_block, phase_name=phase_name, tasks=tasks)

    assert not any(line == _AC_SECTION_HEADING for line in inserted_block), (
        "a direct add-phase call with no agreed Acceptance Criteria must not add an "
        f"'{_AC_SECTION_HEADING}' section, but one was found in: {inserted_block!r}"
    )
    fabricated_items = [
        line
        for line in inserted_block
        if _AC_ITEM_VERIFY_PATTERN.match(line) is not None
        or _AC_ITEM_JUDGE_PATTERN.match(line) is not None
    ]
    assert not fabricated_items, (
        "a direct add-phase call with no agreed Acceptance Criteria must not fabricate any "
        f"verify/judge item, but found: {fabricated_items!r}"
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


def _is_symlink(cwd: Path | None, relative_path: str) -> bool:
    """Return whether `relative_path` (resolved against `cwd`, defaulting to process cwd) is a
    symlink, *without* following it.

    PR #273 final review (security High): with `permission_mode: bypassPermissions` and `Bash`
    exposed, a candidate can run `ln -s <existing file> .claude/handoffs/x.md`. The link's own
    path falls under an allowed prefix, so a path-only allowlist check (this module's prior
    behavior) would pass it through; downstream content oracles that call `.read_text()` then
    transparently follow the link and read whatever it points at, including a file that
    happens to already contain the expected marker text. `Path.is_symlink()` inspects the link
    itself (`lstat`-equivalent; it does not resolve/follow), so it correctly flags the symlink
    even when its target is legitimate content elsewhere in the workspace.
    """
    root = cwd if cwd is not None else Path.cwd()
    return (root / relative_path).is_symlink()


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

    Symlink check (PR #273 final review, security High): regardless of the above, *any* path
    that is itself a symlink is always unexpected, even if its path matches `allowed_paths` or
    `allowed_new_prefixes`. This closes an oracle-bypass path: `ln -s <existing file>
    .claude/handoffs/x.md` puts a path-legitimate but content-illegitimate link under an
    allowed prefix, and a downstream oracle that does `.read_text()` on it would transparently
    follow the link and read whatever pre-existing file it points at. See `_is_symlink`.

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
            and (
                _is_symlink(cwd, path)
                or (
                    path not in allowed_paths
                    and not (status == "A" and path.startswith(allowed_new_prefixes))
                )
            )
        }
    )
    assert not unexpected, (
        f"tracked files changed outside the allowed scope {sorted(allowed_paths)} (or are "
        f"symlinks, which are never allowed): {unexpected}"
    )

    status_tokens = _run_git_z(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=cwd
    )
    untracked = _parse_untracked_paths(status_tokens)
    disallowed_new = [
        path
        for path in untracked
        if _is_symlink(cwd, path)
        or (path not in allowed_paths and not path.startswith(allowed_new_prefixes))
    ]
    assert not disallowed_new, (
        f"new untracked files outside the allowed scope ({sorted(allowed_paths)}) and allowed "
        f"prefixes {sorted(allowed_new_prefixes)} (or are symlinks, which are never allowed): "
        f"{sorted(disallowed_new)}"
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

    add_phase_with_ac = subparsers.add_parser("add-phase-with-ac")
    add_phase_with_ac.add_argument("--plans", type=Path, required=True)
    add_phase_with_ac.add_argument("--phase-name", required=True)
    add_phase_with_ac.add_argument("--tasks", nargs="+", required=True)
    add_phase_with_ac.add_argument("--verify-text", required=True)
    add_phase_with_ac.add_argument("--verify-command", required=True)
    add_phase_with_ac.add_argument("--judge-text", required=True)
    add_phase_with_ac.add_argument("--judge-criteria", required=True)

    add_phase_no_ac = subparsers.add_parser("add-phase-no-ac")
    add_phase_no_ac.add_argument("--plans", type=Path, required=True)
    add_phase_no_ac.add_argument("--phase-name", required=True)
    add_phase_no_ac.add_argument("--tasks", nargs="+", required=True)

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
            "prefix new untracked files (and staged new tracked files, status A) are allowed "
            "to appear under (repeatable). The untracked scan always runs regardless of this "
            "flag (PR #273 bot review round 3); omitting it means no new untracked file is "
            "permitted at all, except paths that exactly match --allow"
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
    elif args.mode == "record-decision":
        assert_decision_recorded(plans_path, expected_decision=args.expected_decision)
    elif args.mode == "add-phase-with-ac":
        assert_add_phase_with_ac(
            plans_path,
            phase_name=args.phase_name,
            tasks=args.tasks,
            verify_text=args.verify_text,
            verify_command=args.verify_command,
            judge_text=args.judge_text,
            judge_criteria=args.judge_criteria,
        )
    else:
        assert_add_phase_no_ac(plans_path, phase_name=args.phase_name, tasks=args.tasks)


if __name__ == "__main__":
    main()
