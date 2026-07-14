#!/usr/bin/env python3
"""LP-2 loop-harness state inspection CLI: list / show / purge / untombstone loop runs.

Reads root-worktree-side `.claude/loop/<loop_id>/state.json` (and `journal.jsonl` for
`show`) written by `loop_step.py` (LP-1) / `loop_driver.py` (LP-2). See
`docs/design/loop-harness-cli.md` 4 節 (authoritative design) for the contract this
module must not deviate from.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_LIB_DIR = _SCRIPT_DIR.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import loop_common as lc  # noqa: E402
import loop_definition as ld  # noqa: E402

DEFAULT_PURGE_AFTER_DAYS = 30
DEFAULT_JOURNAL_LINES = 10

# SN2: suffix for a purged loop's lightweight tombstone file, written directly under
# `.claude/loop/` (never inside a per-loop dir, so it survives that dir's `rmtree`). Mirrors
# `loop_scheduler._EXIT_FOREIGN_LEASE`'s precedent of a shared literal kept local in each
# sibling script instead of a script-to-script import (`loop_scheduler.py` reads this same
# suffix to exclude tombstoned loop_ids from discovery; see its `_tombstoned_loop_ids`).
_TOMBSTONE_SUFFIX = ".tombstone.json"

# 通常 purge の対象（完了として確定した状態のみ）。`stopped`（安全停止）は人間の調査を要するため
# 通常 purge には含めない（EV-52; docs/design/loop-harness-cli.md 4.3 節）。
_PURGEABLE_STATUSES_NORMAL = frozenset({"passed", "failed"})
# `--force` でも purge しない状態（実行中データの誤消去防止）。`pending`（初回 Maker 実行中、
# まだ running に遷移する前）も lock が有効なまま purge されると実行中 run を破損しうるため対象外
# とする（H6 レビュー指摘; docs/design/loop-harness-cli.md 4.3 節）。
#
# 例外（SM1）: ディレクトリ名に `lc.ORPHANED_PENDING_MARKER` を含む退避ディレクトリ
# （`loop_scheduler.recover_orphaned_pending_loops` が lease 失効を確認済みの上で退避したもの）
# は `status` が `pending` のままでも、この保護の対象外として扱う。post-mortem 用の凍結スナップ
# ショットであり、以後 lock/state が更新されることも実行中 run が存在することもないため、
# 通常 purge (30日) / --force のどちらでも安全に削除できる。See `purge_candidates` /
# `_purge_if_state_allows`.
_NEVER_PURGE_STATUSES = frozenset({"pending", "running", "waiting_external"})

_STATUS_CHOICES = (
    "pending",
    "running",
    "waiting_external",
    "passed",
    "failed",
    "stopped",
)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `loop_status.py list|show|purge|untombstone ...`."""
    args = _parse_args(argv)
    project = _project_dir(args.project)
    try:
        return _dispatch(args, project)
    except lc.LoopHarnessError as exc:
        print(f"loop_status: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace, project: str) -> int:
    """Run the subcommand selected by argparse."""
    if args.command == "list":
        summaries = collect_summaries(project, status_filter=args.status)
        print(render_json(summaries) if args.json else render_table(summaries))
        return 0
    if args.command == "show":
        state_dict, journal_entries = load_show_data(
            args.loop_id,
            project,
            journal_lines=args.journal_lines,
            full_journal=args.full_journal,
        )
        print(format_show(state_dict, journal_entries, full_journal=args.full_journal))
        return 0
    if args.command == "purge":
        return _run_purge(args, project)
    if args.command == "untombstone":
        return _run_untombstone(args, project)
    return 1


def _run_untombstone(args: argparse.Namespace, project: str) -> int:
    """Remove a purge tombstone (RM2) so its Issue can be resumed as a deliberate fresh run.

    There is otherwise no operator-facing way to undo a tombstone: `purge`/`--force` never
    re-purge or touch an already-tombstoned loop_id, and `loop_scheduler.discover_loop_ids`
    permanently excludes any tombstoned loop_id from discovery (SN2) - the only prior recourse
    was manually deleting the `<loop_id>.tombstone.json` file on disk. This runs under
    `lc.held_coord_lock` (the same fixed, purge-independent per-`loop_id` lock
    `_purge_if_still_safe`/`resume`/`reacquire_lease`/the scheduler's safety-stop all share,
    see their docstrings), so it cannot race a concurrent purge of the same loop_id recreating
    the tombstone the instant after this removes it.
    """
    try:
        lc._validate_safe_id("loop_id", args.loop_id)  # noqa: SLF001 - reuse shared id-safety guard
    except ValueError as exc:
        print(f"loop_status: {exc}", file=sys.stderr)
        return 1
    with lc.held_coord_lock(args.loop_id, project):
        path = lc.loop_root(project) / f"{args.loop_id}{_TOMBSTONE_SUFFIX}"
        if not path.is_file():
            print(f"loop_status: no tombstone found for {args.loop_id!r}", file=sys.stderr)
            return 1
        path.unlink()
    print(
        f"loop_status: removed tombstone for {args.loop_id!r}; its Issue can now be "
        "discovered/resumed as a fresh run",
        file=sys.stderr,
    )
    return 0


def _run_purge(args: argparse.Namespace, project: str) -> int:
    """Resolve retention config, print candidates, and delete unless --dry-run.

    Real (non-dry-run) deletion requires confirmation: `--yes` skips the prompt;
    otherwise the user must type exactly 'yes'. Any other answer, EOF, or a
    non-interactive stdin aborts the purge with no deletion and a non-zero exit.
    """
    config = ld.load_config(project)
    purge_after_days = int(
        _nested(config, ("retention", "purge_after_days"), DEFAULT_PURGE_AFTER_DAYS)
    )
    candidates = purge_candidates(project, force=args.force, purge_after_days=purge_after_days)
    for loop_id in candidates:
        print(loop_id, file=sys.stderr)
    if args.dry_run:
        return 0
    if candidates and not args.yes and not _confirm_purge(len(candidates)):
        print("loop_status: purge aborted (confirmation declined)", file=sys.stderr)
        return 1
    for loop_id in candidates:
        _purge_if_still_safe(loop_id, project)
    return 0


def _confirm_purge(count: int) -> bool:
    """Prompt for confirmation before a real (non-dry-run) deletion.

    Declines (returns False) when stdin is non-interactive or the prompt hits EOF
    (e.g. Ctrl-D), instead of letting `input()`'s `EOFError` propagate uncaught.
    """
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"Purge {count} loop run(s)? Type 'yes' to confirm: ")
    except EOFError:
        return False
    return answer.strip().lower() == "yes"


def _purge_if_still_safe(loop_id: str, project_dir: str) -> None:
    """Reload state immediately before deletion; skip if it became running/waiting_external.

    Guards against the candidate list going stale between computation and deletion
    (e.g. a loop transitioning back to `running` in that window). Holds the loop's
    lock-file flock across the reload + purge so a concurrent lease (re)acquisition
    (`acquire_lock`/`reacquire_lease`/`resume`, which all take this same lock-file flock
    before writing, see `loop_common._replace_lock`) cannot race with the purge (TOCTOU).

    A `failed` loop's lease was already released (`release_lock` deletes lock.json), so the
    common case here finds no lock file at all. Rather than falling back to an unlocked
    check-then-purge (which would leave nothing to serialize against a concurrent
    `resume()`), a placeholder lock file is created under `O_CREAT | O_EXCL` first purely to
    have a path both sides can flock. If it turns out purge must be skipped (state changed
    out from under it), the placeholder is removed again so no stray lock.json is left
    behind for a loop this call did not actually purge.

    SN-flock: the whole reload-through-purge section is additionally wrapped in
    `lc.held_coord_lock` (a fixed, purge-independent path - see its docstring). The inner
    lock-file flock above stops protecting anything the instant `purge_loop`'s `rmtree`
    deletes that lock.json's inode: a concurrent `resume`/`reacquire_lease` racing this call
    would recreate a brand-new lock.json (a different inode) and never contend with the flock
    still (uselessly) held on the deleted one. The outer coord lock is what actually closes
    that race; the inner lock-file flock is left in place unchanged for its own existing
    contract/tests (double-locking on two different files is not a deadlock risk - `flock()`
    contention is per-file, not per-process).
    """
    with lc.held_coord_lock(loop_id, project_dir):
        lock_file = lc.lock_path(loop_id, project_dir)
        created_placeholder = False
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, lc.FILE_MODE)
            created_placeholder = True
        except FileExistsError:
            fd = lc._open_lock_for_update(lock_file)  # noqa: SLF001 - reuse shared lock-open helper
        if fd is None:
            _purge_if_state_allows(loop_id, project_dir)
            return
        with os.fdopen(fd, "r+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            purged = _purge_if_state_allows(loop_id, project_dir)
        if created_placeholder and not purged and lock_file.exists():
            lock_file.unlink()


def _purge_if_state_allows(loop_id: str, project_dir: str) -> bool:
    """Reload state and purge unless it is currently running/waiting_external.

    `loop_id` here is a directory name from `_iter_loop_dirs`/`purge_candidates`, which for a
    retired orphaned-pending dir (SM1) contains `lc.ORPHANED_PENDING_MARKER` and is exempt
    from the `_NEVER_PURGE_STATUSES` `pending` guard: its lease was already verified expired
    before `loop_scheduler.recover_orphaned_pending_loops` renamed it aside, so - unlike a
    live `pending` loop - it carries no in-flight run left to protect, and its recorded
    `status` field is simply frozen at whatever it was at retirement time (always `pending`).

    Returns True when `purge_loop` actually ran (directory deleted), False when skipped.
    """
    state = _try_load_state(loop_id, project_dir)
    orphaned = lc.ORPHANED_PENDING_MARKER in loop_id
    if state is not None and not orphaned and state.status in _NEVER_PURGE_STATUSES:
        return False
    purge_loop(loop_id, project_dir)
    return True


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse loop_status.py CLI arguments."""
    parser = argparse.ArgumentParser(prog="loop_status.py", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list loop runs")
    list_parser.add_argument("--project")
    list_parser.add_argument("--status", choices=_STATUS_CHOICES)
    list_parser.add_argument("--json", action="store_true")

    show_parser = subparsers.add_parser("show", help="show one loop run's state + journal")
    show_parser.add_argument("--loop-id", required=True)
    show_parser.add_argument("--project")
    show_parser.add_argument("--journal-lines", type=int, default=DEFAULT_JOURNAL_LINES)
    show_parser.add_argument("--full-journal", action="store_true")

    purge_parser = subparsers.add_parser("purge", help="purge completed loop runs")
    purge_parser.add_argument("--project")
    purge_parser.add_argument("--force", action="store_true")
    purge_parser.add_argument("--dry-run", action="store_true")
    purge_parser.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt for real deletion"
    )

    untombstone_parser = subparsers.add_parser(
        "untombstone",
        help="remove a purge tombstone (RM2) so the Issue can be resumed as a fresh run",
    )
    untombstone_parser.add_argument("--loop-id", required=True)
    untombstone_parser.add_argument("--project")

    return parser.parse_args(argv)


def _project_dir(value: str | None) -> str:
    """Resolve project dir from --project or cwd's git root."""
    if value:
        return str(Path(value).resolve())
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    raise lc.RootResolutionError("could not find git repository root")


def _nested(source: dict[str, Any], path: tuple[str, ...], default: Any) -> Any:
    """Read a nested mapping value, falling back to default on any miss."""
    current: Any = source
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


# -- shared traversal helpers ---------------------------------------------------------------


def _iter_loop_dirs(project_dir: str) -> list[Path]:
    """Return sorted `.claude/loop/<loop_id>/` directories (root-worktree side)."""
    root = lc.loop_root(project_dir)
    if not root.is_dir():
        return []
    return sorted(entry for entry in root.iterdir() if entry.is_dir())


def _try_load_state(loop_id: str, project_dir: str) -> lc.LoopState | None:
    """Load one loop's state.json, or None if absent/corrupted."""
    try:
        return lc.load_state(loop_id, project_dir)
    except lc.LoopHarnessError:
        return None


# -- 4.1 節: list -----------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopSummary:
    """One row of `loop_status.py list` output."""

    loop_id: str
    definition_id: str
    phase: str
    iteration: int
    max_iterations: int
    status: str
    created_at: str
    updated_at: str
    pr_number: int | None


def collect_summaries(project_dir: str, *, status_filter: str | None = None) -> list[LoopSummary]:
    """Collect one LoopSummary per loop run, optionally filtered by status.

    SN2: also includes one synthetic row per purge tombstone, so a purged loop still shows up
    in `list` (with its frozen-at-purge-time terminal status) instead of vanishing without a
    trace the moment its directory is deleted.
    """
    summaries: list[LoopSummary] = []
    for entry in _iter_loop_dirs(project_dir):
        state = _try_load_state(entry.name, project_dir)
        if state is None:
            continue
        if status_filter is not None and state.status != status_filter:
            continue
        summaries.append(_summary_from_state(state, project_dir, entry.name))
    for tombstone in _load_tombstones(project_dir):
        if status_filter is not None and tombstone["status"] != status_filter:
            continue
        summaries.append(_summary_from_tombstone(tombstone))
    return summaries


def _load_tombstones(project_dir: str) -> list[dict[str, str]]:
    """Read all valid purge tombstones (SN2) under `.claude/loop/`, sorted by loop_id."""
    root = lc.loop_root(project_dir)
    if not root.is_dir():
        return []
    tombstones: list[dict[str, str]] = []
    for path in sorted(root.glob(f"*{_TOMBSTONE_SUFFIX}")):
        tombstone = _try_load_tombstone(path)
        if tombstone is not None:
            tombstones.append(tombstone)
    return tombstones


def _try_load_tombstone(path: Path) -> dict[str, str] | None:
    """Read one tombstone file, or None if absent/malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    loop_id, status_value, purged_at = (
        data.get("loop_id"),
        data.get("status"),
        data.get("purged_at"),
    )
    if not all(isinstance(v, str) and v for v in (loop_id, status_value, purged_at)):
        return None
    return {"loop_id": loop_id, "status": status_value, "purged_at": purged_at}


def _summary_from_tombstone(tombstone: dict[str, str]) -> LoopSummary:
    """Build a synthetic LoopSummary for a purged (tombstoned) loop_id (SN2).

    Per-phase/iteration/definition detail is gone along with the purged state.json; only the
    identifier and its frozen-at-purge-time status are still meaningful, so those fields use
    sentinel/zero values rather than inventing data that no longer exists.
    """
    return LoopSummary(
        loop_id=tombstone["loop_id"],
        definition_id="",
        phase="purged",
        iteration=0,
        max_iterations=0,
        status=tombstone["status"],
        created_at=tombstone["purged_at"],
        updated_at=tombstone["purged_at"],
        pr_number=None,
    )


def _summary_from_state(state: lc.LoopState, project_dir: str, dir_name: str) -> LoopSummary:
    """Build a LoopSummary from a loaded LoopState.

    `dir_name` (the actual `.claude/loop/<dir_name>/` directory name), not `state.loop_id`,
    is used as the displayed identifier (SM1): a retired orphaned-pending dir
    (`lc.ORPHANED_PENDING_MARKER` in its name, see `loop_scheduler.recover_orphaned_pending_loops`)
    still carries the *original* (no-suffix) `loop_id` inside its frozen `state.json`, which
    would otherwise display identically to - and be indistinguishable from - a live loop for
    the same Issue. For a normal (non-orphaned) dir, `dir_name` and `state.loop_id` are the
    same value, so this is a no-op for the common case.
    """
    return LoopSummary(
        loop_id=dir_name,
        definition_id=state.definition_id,
        phase=state.phase,
        # `state.iteration` is round-tripped by `loop_common._state_to_dict` from the
        # current phase's guard counter (or the pending action's iteration, if any); it
        # is already the "current iteration" this table wants, not a stale/unused field.
        iteration=state.iteration,
        max_iterations=_max_iterations_for(state, project_dir),
        status=state.status,
        created_at=state.created_at,
        updated_at=state.updated_at,
        pr_number=state.pr_number,
    )


def _max_iterations_for(state: lc.LoopState, project_dir: str) -> int:
    """Resolve `guards.max_iterations` for the loop's current phase (definition override or config)."""
    config = ld.load_config(project_dir)
    phase_def = _phase_definition_or_none(state, project_dir)
    return lc._phase_max_iterations(phase_def, config)  # noqa: SLF001 - shared guard-resolution logic


def _phase_definition_or_none(state: lc.LoopState, project_dir: str) -> Any | None:
    """Best-effort phase definition lookup; None if the definition/phase is unavailable."""
    try:
        definition = ld.load_all_definitions(project_dir)[state.definition_id]
        return ld.phase_by_name(definition, state.phase)
    except (KeyError, ld.DefinitionValidationError):
        return None


def _elapsed_hhmmss(created_at: str, updated_at: str) -> str:
    """Return `updated_at - created_at` formatted as `HH:MM:SS`.

    Also tolerates `TypeError` (M6): a tz-aware/naive `datetime` mismatch raises `TypeError`
    on subtraction, not `ValueError`, and must fall back the same way a parse failure does.
    """
    try:
        created = datetime.fromisoformat(created_at)
        updated = datetime.fromisoformat(updated_at)
        total_seconds = max(int((updated - created).total_seconds()), 0)
    except (ValueError, TypeError):
        return "00:00:00"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def render_table(summaries: list[LoopSummary]) -> str:
    """Render summaries as the human-readable `LOOP_ID PHASE ITERATION STATUS ELAPSED` table."""
    header = f"{'LOOP_ID':<26}{'PHASE':<22}{'ITERATION':<11}{'STATUS':<18}{'ELAPSED'}"
    lines = [header]
    for summary in summaries:
        iteration = f"{summary.iteration}/{summary.max_iterations}"
        elapsed = _elapsed_hhmmss(summary.created_at, summary.updated_at)
        lines.append(
            f"{summary.loop_id:<26}{summary.phase:<22}{iteration:<11}{summary.status:<18}{elapsed}"
        )
    return "\n".join(lines)


def render_json(summaries: list[LoopSummary]) -> str:
    """Render summaries as the `--json` array (4.1 節 schema)."""
    return json.dumps(
        [
            {
                "loop_id": s.loop_id,
                "definition_id": s.definition_id,
                "phase": s.phase,
                "iteration": s.iteration,
                "max_iterations": s.max_iterations,
                "status": s.status,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "pr_number": s.pr_number,
            }
            for s in summaries
        ],
        ensure_ascii=False,
        indent=2,
    )


# -- 4.2 節: show -----------------------------------------------------------------------------


def _read_journal_entries(loop_id: str, project_dir: str) -> list[dict[str, Any]]:
    """Read all journal.jsonl entries in file order (oldest first); skip malformed lines."""
    path = lc.journal_path(loop_id, project_dir)
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entries.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    return entries


def load_show_data(
    loop_id: str,
    project_dir: str,
    *,
    journal_lines: int = DEFAULT_JOURNAL_LINES,
    full_journal: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (state.json dict, selected journal entries) for `show` (raises if loop_id unknown)."""
    state = lc.load_state(loop_id, project_dir)
    state_dict = lc._state_to_dict(state)  # noqa: SLF001 - reuse the canonical serialization
    entries = _read_journal_entries(loop_id, project_dir)
    if not full_journal and journal_lines >= 0:
        entries = entries[-journal_lines:] if journal_lines > 0 else []
    return state_dict, entries


def format_show(
    state_dict: dict[str, Any],
    journal_entries: list[dict[str, Any]],
    *,
    full_journal: bool,
) -> str:
    """Format `show` output: full state.json content, then journal entries in time order."""
    state_text = json.dumps(state_dict, ensure_ascii=False, indent=2, sort_keys=True)
    label = "full journal" if full_journal else f"last {len(journal_entries)} journal entries"
    journal_text = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in journal_entries)
    return f"{state_text}\n\n--- {label} ---\n{journal_text}"


# -- 4.3 節: purge ----------------------------------------------------------------------------


def purge_candidates(
    project_dir: str,
    *,
    force: bool,
    purge_after_days: int,
    now: datetime | None = None,
) -> list[str]:
    """Return loop_ids eligible for purge under the given mode (EV-52).

    Returns each dir's actual directory name (not `state.loop_id`), which matters for a
    retired orphaned-pending dir (SM1): its `state.json` still carries the *original*
    (no-suffix) `loop_id`, but the directory itself lives at the renamed-aside path. Passing
    the original loop_id to `purge_loop` would resolve to a directory that no longer exists
    (a no-op) while the orphaned dir itself is silently skipped - the same mismatch bug fixed
    for `verify_repo_identity_at_startup` (SH1). For a normal (non-orphaned) dir, directory
    name and `state.loop_id` are the same value, so this is a no-op change for the common case.
    """
    now = now or datetime.now(tz=UTC)
    candidates: list[str] = []
    for entry in _iter_loop_dirs(project_dir):
        state = _try_load_state(entry.name, project_dir)
        if state is None:
            continue
        orphaned = lc.ORPHANED_PENDING_MARKER in entry.name
        if not orphaned and state.status in _NEVER_PURGE_STATUSES:
            continue
        if force:
            candidates.append(entry.name)
            continue
        purgeable_status = orphaned or state.status in _PURGEABLE_STATUSES_NORMAL
        if purgeable_status and _days_since(state.updated_at, now) >= purge_after_days:
            candidates.append(entry.name)
    return candidates


def _days_since(updated_at: str, now: datetime) -> float:
    """Return the number of days elapsed between updated_at and now (0.0 on parse failure).

    Also tolerates `TypeError` (M6): a tz-aware/naive `datetime` mismatch raises `TypeError`
    on subtraction, not `ValueError`.
    """
    try:
        updated = datetime.fromisoformat(updated_at)
        return (now - updated).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 0.0


def purge_loop(loop_id: str, project_dir: str) -> None:
    """Delete a loop run's state.json/journal.jsonl/artifacts/ (worktree itself is untouched).

    Deletion failures (permissions, locked files, etc.) propagate as `LoopHarnessError`
    instead of being silently swallowed, so the CLI exits non-zero on partial purges.
    A missing directory (already purged/never existed) is treated as a no-op (no tombstone
    write is attempted in that case either - nothing was actually purged by this call).

    RH3: the tombstone is written *before* `rmtree`, not after (the previous ordering), and
    atomically (temp file + `os.replace`, via the shared `lc._write_text` writer) rather than
    a direct `path.write_text`. The whole call already runs under `lc.held_coord_lock`
    (`_purge_if_still_safe`), so this reordering does not introduce any new race with a
    concurrent `resume`/`attach` - but it does close a different, purge-internal gap: the
    previous rmtree-then-write ordering left a window where the directory was already gone but
    the tombstone had not yet landed (or, with a non-atomic write, could land truncated on a
    mid-write crash). A crash/kill in that exact window left neither a live state.json nor a
    valid tombstone behind, so `loop_scheduler.discover_loop_ids` would treat the already-
    purged Issue as a brand-new candidate on the very next poll cycle - defeating the point of
    purging it. Publishing the tombstone atomically first means it is durably in place before
    any deletion happens at all; a tombstone I/O failure now raises `LoopHarnessError` (via
    `_write_tombstone`) and this function returns before ever calling `rmtree`.

    RH2/SN2: no tombstone is written when `loop_id` is a retired orphaned-pending snapshot dir
    (`lc.ORPHANED_PENDING_MARKER` in its *directory name*; see
    `loop_scheduler.recover_orphaned_pending_loops`). Such a dir's frozen `state.json` still
    carries the *original* (no-suffix) `loop_id`, but the coord lock this call runs under
    (`_purge_if_still_safe`/`held_coord_lock`) is keyed on the directory name passed in here,
    not on that original loop_id. Writing a tombstone under the original loop_id from inside a
    lock keyed on the snapshot's own (different) name would tombstone the original loop_id
    without ever holding *its* coord lock - racing a concurrently resumed/re-spawned run for the
    same Issue that legitimately does hold it. The snapshot itself is simply deleted here; the
    original loop's own terminal record (tombstone) is written only if/when that original loop
    is itself later purged, under its own (matching) coord lock.

    Future extension (out of scope here): a `--with-worktree` flag to also remove the
    associated `.worktrees/loop-issue-<N>` directory via `worktree_manager.remove_worktree`
    (docs/design/loop-harness-cli.md 4.3 節).
    """
    target = lc.loop_dir(loop_id, project_dir)
    if not target.is_dir():
        return
    if lc.ORPHANED_PENDING_MARKER not in loop_id:
        state = _try_load_state(loop_id, project_dir)
        _write_tombstone(loop_id, project_dir, state)
    try:
        shutil.rmtree(target)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise lc.LoopHarnessError(f"failed to purge loop {loop_id!r}: {exc}") from exc


def _write_tombstone(loop_id: str, project_dir: str, state: lc.LoopState | None) -> None:
    """Atomically publish a minimal purge tombstone (SN2): `{loop_id, status, purged_at}`.

    Uses `state.loop_id` (the durable identifier `loop_scheduler.discover_loop_ids` actually
    compares candidates against) rather than the `loop_id` argument here - falls back to the
    argument itself only when no state could be loaded (best effort; still prevents immediate
    re-discovery for the common case where state.json was readable). `purge_loop` never calls
    this for an orphaned-pending snapshot dir (RH2, see its docstring), so `loop_id` here is
    always a real, no-suffix loop_id already equal to `state.loop_id` whenever `state` loaded.

    RH3: published via `lc._write_text` (temp file + `os.replace`, the same atomic-write helper
    `state.json`/`journal.jsonl` use) instead of a direct `path.write_text`, so a crash mid-write
    can never leave a truncated tombstone at the final path. Raises `LoopHarnessError` (instead
    of letting a raw `OSError` propagate) on failure, so `purge_loop` can rely on this call
    either fully succeeding or raising - never partially writing - before it goes on to `rmtree`.
    """
    real_loop_id = state.loop_id if state is not None else loop_id
    payload = {
        "loop_id": real_loop_id,
        "status": state.status if state is not None else "unknown",
        "purged_at": lc.now_iso(),
    }
    path = lc.loop_root(project_dir) / f"{real_loop_id}{_TOMBSTONE_SUFFIX}"
    try:
        lc._write_text(path, json.dumps(payload, ensure_ascii=False))  # noqa: SLF001 - reuse shared atomic writer (RH3)
    except OSError as exc:
        raise lc.LoopHarnessError(
            f"failed to write purge tombstone for {real_loop_id!r}: {exc}"
        ) from exc


if __name__ == "__main__":
    sys.exit(main())
