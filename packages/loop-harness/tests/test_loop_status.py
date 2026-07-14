"""Tests for the LP-2 state inspection CLI (`loop_status.py`).

Covers `list` (table + `--json` + `--status` filter), `show` (state + recent/full
journal), and `purge` (30-day/`--force` boundaries, `running`/`waiting_external`
protection), per the evaluation set (EV-52) and the handoff's required coverage list.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.module_loader import load_module

lc = load_module("loop_common", "packages/loop-harness/lib/loop_common.py")
ld = load_module("loop_definition", "packages/loop-harness/lib/loop_definition.py")
status = load_module("loop_status", "packages/loop-harness/scripts/loop_status.py")


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "loop-harness@example.com"], path)
    _git(["config", "user.name", "Loop Harness Test"], path)
    (path / "README.md").write_text("root\n", encoding="utf-8")
    _git(["add", "README.md"], path)
    _git(["commit", "-m", "init"], path)


def _seed_state(
    tmp_path: Path,
    loop_id: str,
    *,
    status_value: str = "running",
    phase: str = "implementation",
    updated_at: str | None = None,
) -> lc.LoopState:
    """Write a minimal state.json for loop_id, optionally back-dating updated_at."""
    project_dir = str(tmp_path)
    state = lc._initial_state(loop_id, "issue-loop", "abcd1234", project_dir, "main", phase)
    state.status = status_value
    if updated_at is not None:
        state.updated_at = updated_at
    lc._write_state(state, project_dir)
    return state


def _iso(days_ago: float) -> str:
    return (datetime.now(tz=UTC) - timedelta(days=days_ago)).isoformat()


# --------------------------------------------------------------------------------------------
# 4.1 節: list
# --------------------------------------------------------------------------------------------


def test_collect_summaries_reads_all_loops_with_iteration_and_max_iterations(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _seed_state(tmp_path, "abcd1234-issue-1", status_value="running")
    _seed_state(tmp_path, "abcd1234-issue-2", status_value="passed")

    summaries = status.collect_summaries(str(tmp_path))

    by_id = {s.loop_id: s for s in summaries}
    assert set(by_id) == {"abcd1234-issue-1", "abcd1234-issue-2"}
    assert by_id["abcd1234-issue-1"].status == "running"
    assert by_id["abcd1234-issue-1"].definition_id == "issue-loop"
    assert by_id["abcd1234-issue-1"].phase == "implementation"
    # freshly-initialized state: no iterations recorded yet, phase cap from issue-loop.yaml
    assert by_id["abcd1234-issue-1"].iteration == 0
    assert by_id["abcd1234-issue-1"].max_iterations == 3


def test_collect_summaries_status_filter(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed_state(tmp_path, "abcd1234-issue-1", status_value="running")
    _seed_state(tmp_path, "abcd1234-issue-2", status_value="passed")

    summaries = status.collect_summaries(str(tmp_path), status_filter="passed")

    assert [s.loop_id for s in summaries] == ["abcd1234-issue-2"]


def test_render_table_includes_expected_columns(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed_state(tmp_path, "abcd1234-issue-1", status_value="running")
    summaries = status.collect_summaries(str(tmp_path))

    table = status.render_table(summaries)

    assert "LOOP_ID" in table
    assert "PHASE" in table
    assert "ITERATION" in table
    assert "STATUS" in table
    assert "ELAPSED" in table
    assert "abcd1234-issue-1" in table
    assert "0/3" in table
    assert "running" in table


def test_render_json_round_trips_expected_fields(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed_state(tmp_path, "abcd1234-issue-1", status_value="waiting_external")
    summaries = status.collect_summaries(str(tmp_path))

    data = json.loads(status.render_json(summaries))

    assert data == [
        {
            "loop_id": "abcd1234-issue-1",
            "definition_id": "issue-loop",
            "phase": "implementation",
            "iteration": 0,
            "max_iterations": 3,
            "status": "waiting_external",
            "created_at": data[0]["created_at"],
            "updated_at": data[0]["updated_at"],
            "pr_number": None,
        }
    ]


def test_elapsed_hhmmss_formats_duration() -> None:
    created = "2026-07-06T10:00:00+00:00"
    updated = "2026-07-06T10:14:32+00:00"
    assert status._elapsed_hhmmss(created, updated) == "00:14:32"


def test_elapsed_hhmmss_falls_back_on_tz_aware_naive_mismatch() -> None:
    """M6: tz-aware minus tz-naive raises TypeError (not ValueError) on subtraction."""
    created = "2026-07-06T10:00:00+00:00"
    updated = "2026-07-06T10:14:32"  # naive (no tz offset)
    assert status._elapsed_hhmmss(created, updated) == "00:00:00"


def test_days_since_falls_back_on_tz_aware_naive_mismatch() -> None:
    """M6: same TypeError fallback for `_days_since`."""
    from datetime import UTC, datetime

    now = datetime(2026, 7, 6, tzinfo=UTC)
    assert status._days_since("2026-07-01T00:00:00", now) == 0.0


def test_main_list_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _init_repo(tmp_path)
    _seed_state(tmp_path, "abcd1234-issue-1", status_value="running")

    exit_code = status.main(["list", "--project", str(tmp_path), "--json"])

    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["loop_id"] == "abcd1234-issue-1"


# --------------------------------------------------------------------------------------------
# 4.2 節: show
# --------------------------------------------------------------------------------------------


def test_load_show_data_defaults_to_last_10_journal_entries(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    loop_id = "abcd1234-issue-1"
    _seed_state(tmp_path, loop_id, status_value="running")
    for index in range(15):
        lc.append_journal_event(loop_id, str(tmp_path), "pending", "step", f"act-{index}", {})

    state_dict, entries = status.load_show_data(loop_id, str(tmp_path))

    assert state_dict["loop_id"] == loop_id
    assert len(entries) == 10
    assert entries[-1]["action_id"] == "act-14"
    assert entries[0]["action_id"] == "act-5"


def test_load_show_data_custom_journal_lines(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    loop_id = "abcd1234-issue-1"
    _seed_state(tmp_path, loop_id, status_value="running")
    for index in range(5):
        lc.append_journal_event(loop_id, str(tmp_path), "pending", "step", f"act-{index}", {})

    _state_dict, entries = status.load_show_data(loop_id, str(tmp_path), journal_lines=2)

    assert [entry["action_id"] for entry in entries] == ["act-3", "act-4"]


def test_load_show_data_full_journal_returns_all_entries(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    loop_id = "abcd1234-issue-1"
    _seed_state(tmp_path, loop_id, status_value="running")
    for index in range(15):
        lc.append_journal_event(loop_id, str(tmp_path), "pending", "step", f"act-{index}", {})

    _state_dict, entries = status.load_show_data(loop_id, str(tmp_path), full_journal=True)

    assert len(entries) == 15


def test_load_show_data_raises_for_unknown_loop_id(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    with pytest.raises(lc.InvalidStateError):
        status.load_show_data("no-such-loop", str(tmp_path))


def test_format_show_includes_state_and_journal_label() -> None:
    text = status.format_show(
        {"loop_id": "abcd1234-issue-1"}, [{"event": "pending"}], full_journal=False
    )
    assert '"loop_id": "abcd1234-issue-1"' in text
    assert "last 1 journal entries" in text
    assert '"event": "pending"' in text


def test_main_show_raises_reports_error_and_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    exit_code = status.main(["show", "--loop-id", "no-such-loop", "--project", str(tmp_path)])
    assert exit_code == 1
    assert "loop_status:" in capsys.readouterr().err


# --------------------------------------------------------------------------------------------
# 4.3 節: purge (EV-52)
# --------------------------------------------------------------------------------------------


def test_purge_candidates_normal_selects_only_passed_failed_past_retention(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _seed_state(tmp_path, "a-issue-1", status_value="passed", updated_at=_iso(31))
    _seed_state(tmp_path, "a-issue-2", status_value="failed", updated_at=_iso(40))
    _seed_state(tmp_path, "a-issue-3", status_value="passed", updated_at=_iso(5))  # too recent
    _seed_state(tmp_path, "a-issue-4", status_value="stopped", updated_at=_iso(60))  # never normal
    _seed_state(tmp_path, "a-issue-5", status_value="running", updated_at=_iso(100))  # never

    candidates = status.purge_candidates(str(tmp_path), force=False, purge_after_days=30)

    assert set(candidates) == {"a-issue-1", "a-issue-2"}


def test_purge_candidates_boundary_at_exactly_purge_after_days(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed_state(tmp_path, "a-issue-1", status_value="passed", updated_at=_iso(30))

    candidates = status.purge_candidates(str(tmp_path), force=False, purge_after_days=30)

    assert candidates == ["a-issue-1"]


def test_purge_candidates_force_includes_stopped_but_protects_running_and_waiting(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _seed_state(tmp_path, "a-issue-1", status_value="passed", updated_at=_iso(1))
    _seed_state(tmp_path, "a-issue-2", status_value="stopped", updated_at=_iso(1))
    _seed_state(tmp_path, "a-issue-3", status_value="running", updated_at=_iso(1000))
    _seed_state(tmp_path, "a-issue-4", status_value="waiting_external", updated_at=_iso(1000))

    candidates = status.purge_candidates(str(tmp_path), force=True, purge_after_days=30)

    assert set(candidates) == {"a-issue-1", "a-issue-2"}


def test_purge_candidates_force_never_includes_pending(tmp_path: Path) -> None:
    """H6: `pending` (initial Maker run, before the first `running` transition) must be
    protected from `--force` purge just like `running`/`waiting_external`, since the lock
    is still live and a mid-run purge would corrupt the run."""
    _init_repo(tmp_path)
    _seed_state(tmp_path, "a-issue-1", status_value="pending", updated_at=_iso(1000))
    _seed_state(tmp_path, "a-issue-2", status_value="passed", updated_at=_iso(1))

    candidates = status.purge_candidates(str(tmp_path), force=True, purge_after_days=30)

    assert set(candidates) == {"a-issue-2"}


def test_purge_candidates_includes_orphaned_pending_dir_past_retention(tmp_path: Path) -> None:
    """SM1: a retired orphaned-pending dir (`lc.ORPHANED_PENDING_MARKER` in its dir name, see
    `loop_scheduler.recover_orphaned_pending_loops`) must not accumulate forever just because
    its frozen `status` field is still `pending` (normally protected by `_NEVER_PURGE_STATUSES`).
    Its lease was already verified expired before being renamed aside, so it is safe to include
    in the normal (30-day) purge, using the *directory name* (not `state.loop_id`, which still
    holds the original no-suffix value) as the candidate id."""
    _init_repo(tmp_path)
    original_loop_id = "a-issue-1"
    _seed_state(tmp_path, original_loop_id, status_value="pending", updated_at=_iso(40))
    loop_dir = lc.loop_dir(original_loop_id, str(tmp_path))
    orphaned_name = f"{original_loop_id}{lc.ORPHANED_PENDING_MARKER}1"
    loop_dir.rename(loop_dir.parent / orphaned_name)

    candidates = status.purge_candidates(str(tmp_path), force=False, purge_after_days=30)

    assert candidates == [orphaned_name]


def test_purge_candidates_excludes_orphaned_pending_dir_within_retention(tmp_path: Path) -> None:
    """SM1: the orphaned-pending exemption only lifts the `pending` status guard; the normal
    30-day age check still applies."""
    _init_repo(tmp_path)
    original_loop_id = "a-issue-1"
    _seed_state(tmp_path, original_loop_id, status_value="pending", updated_at=_iso(5))
    loop_dir = lc.loop_dir(original_loop_id, str(tmp_path))
    orphaned_name = f"{original_loop_id}{lc.ORPHANED_PENDING_MARKER}1"
    loop_dir.rename(loop_dir.parent / orphaned_name)

    candidates = status.purge_candidates(str(tmp_path), force=False, purge_after_days=30)

    assert candidates == []


def test_purge_candidates_force_includes_orphaned_pending_dir(tmp_path: Path) -> None:
    """SM1: `--force` must also include a retired orphaned-pending dir, unlike a live `pending`
    loop (still protected, see `test_purge_candidates_force_never_includes_pending`)."""
    _init_repo(tmp_path)
    original_loop_id = "a-issue-1"
    _seed_state(tmp_path, original_loop_id, status_value="pending", updated_at=_iso(1))
    loop_dir = lc.loop_dir(original_loop_id, str(tmp_path))
    orphaned_name = f"{original_loop_id}{lc.ORPHANED_PENDING_MARKER}1"
    loop_dir.rename(loop_dir.parent / orphaned_name)

    candidates = status.purge_candidates(str(tmp_path), force=True, purge_after_days=30)

    assert candidates == [orphaned_name]


def test_purge_orphaned_pending_dir_actually_deletes_it(tmp_path: Path) -> None:
    """SM1 end-to-end: `_purge_if_still_safe` on an orphaned dir id must not be blocked by the
    `_NEVER_PURGE_STATUSES` re-check (its frozen `status` is still `pending`), and must purge
    the *renamed* directory itself, not resolve back to the (already-gone) original path."""
    _init_repo(tmp_path)
    original_loop_id = "a-issue-1"
    _seed_state(tmp_path, original_loop_id, status_value="pending", updated_at=_iso(40))
    loop_dir = lc.loop_dir(original_loop_id, str(tmp_path))
    orphaned_name = f"{original_loop_id}{lc.ORPHANED_PENDING_MARKER}1"
    orphaned_dir = loop_dir.parent / orphaned_name
    loop_dir.rename(orphaned_dir)

    status._purge_if_still_safe(orphaned_name, str(tmp_path))

    assert not orphaned_dir.exists()


def test_collect_summaries_displays_orphaned_dir_name_not_original_loop_id(
    tmp_path: Path,
) -> None:
    """SM1: a retired orphaned-pending dir must be displayed under its actual directory name
    (suffix included), not the original (no-suffix) `loop_id` still recorded in its frozen
    state.json - otherwise it is indistinguishable in `list` output from a live loop for the
    same Issue."""
    _init_repo(tmp_path)
    original_loop_id = "a-issue-1"
    _seed_state(tmp_path, original_loop_id, status_value="pending")
    loop_dir = lc.loop_dir(original_loop_id, str(tmp_path))
    orphaned_name = f"{original_loop_id}{lc.ORPHANED_PENDING_MARKER}1"
    loop_dir.rename(loop_dir.parent / orphaned_name)

    summaries = status.collect_summaries(str(tmp_path))

    assert [s.loop_id for s in summaries] == [orphaned_name]


def test_purge_loop_removes_loop_directory(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))
    assert lc.loop_dir(loop_id, str(tmp_path)).is_dir()

    status.purge_loop(loop_id, str(tmp_path))

    assert not lc.loop_dir(loop_id, str(tmp_path)).exists()


def test_purge_loop_writes_tombstone(tmp_path: Path) -> None:
    """SN2: a purge must leave a lightweight tombstone behind so
    `loop_scheduler.discover_loop_ids` treats the loop_id as terminal (see
    `test_loop_scheduler.py::test_discover_loop_ids_excludes_tombstoned_loops`), instead of
    re-spawning the same Issue the moment its label is (re-)detected. RH3: the tombstone is
    actually published *before* `rmtree` now (see `purge_loop`'s docstring), but this end-state
    assertion (tombstone present with the right payload once `purge_loop` returns) is unaffected
    by that internal ordering flip."""
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))

    status.purge_loop(loop_id, str(tmp_path))

    tombstone_path = lc.loop_root(str(tmp_path)) / f"{loop_id}.tombstone.json"
    assert tombstone_path.is_file()
    payload = json.loads(tombstone_path.read_text(encoding="utf-8"))
    assert payload["loop_id"] == loop_id
    assert payload["status"] == "passed"
    assert payload["purged_at"]


def test_purge_loop_tombstone_persists_when_rmtree_fails_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RH3: the tombstone is published *before* `rmtree` runs (deliberate ordering flip from
    the previous rmtree-then-tombstone behavior), so a `rmtree` failure that happens after a
    successful tombstone publish still raises `LoopHarnessError` (unchanged), but the tombstone
    it already wrote is not rolled back. See `purge_loop`'s RH3 docstring for why a durable
    tombstone-before-delete is preferred here over the small residual risk of the directory and
    its tombstone briefly co-existing."""
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(status.shutil, "rmtree", _boom)

    with pytest.raises(lc.LoopHarnessError):
        status.purge_loop(loop_id, str(tmp_path))

    tombstone_path = lc.loop_root(str(tmp_path)) / f"{loop_id}.tombstone.json"
    assert tombstone_path.is_file()
    payload = json.loads(tombstone_path.read_text(encoding="utf-8"))
    assert payload["loop_id"] == loop_id


def test_purge_loop_skips_rmtree_when_tombstone_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RH3: a tombstone-publish failure must raise `LoopHarnessError` *and* skip `rmtree`
    entirely - the tombstone is written first specifically so a failure here can never result
    in a deleted directory with no durable terminal record left behind at all."""
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))

    def _rmtree_must_not_be_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("rmtree must not run when the tombstone write fails")

    def _boom_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(status.shutil, "rmtree", _rmtree_must_not_be_called)
    monkeypatch.setattr(status.os, "replace", _boom_replace)

    with pytest.raises(lc.LoopHarnessError):
        status.purge_loop(loop_id, str(tmp_path))

    assert lc.loop_dir(loop_id, str(tmp_path)).is_dir()
    tombstone_path = lc.loop_root(str(tmp_path)) / f"{loop_id}.tombstone.json"
    assert not tombstone_path.exists()


def test_write_tombstone_publishes_atomically_via_temp_file_and_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RH3: `_write_tombstone` must publish through a temp file + `os.replace` (the shared
    `lc._write_text` atomic-write helper also used for state.json/journal.jsonl), not a direct
    `path.write_text` - pins the call actually goes through it rather than regressing to a
    non-atomic write that could leave a truncated tombstone behind on a mid-write crash."""
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))

    replace_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def _spy_replace(src: object, dst: object) -> None:
        replace_calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(status.os, "replace", _spy_replace)

    status.purge_loop(loop_id, str(tmp_path))

    tombstone_path = lc.loop_root(str(tmp_path)) / f"{loop_id}.tombstone.json"
    assert tombstone_path.is_file()
    assert len(replace_calls) == 1
    src, dst = replace_calls[0]
    assert dst == str(tombstone_path)
    assert src != dst
    assert ".tmp." in src


def test_purge_loop_does_not_write_tombstone_for_orphaned_snapshot_dir(
    tmp_path: Path,
) -> None:
    """RH2: purging a retired orphaned-pending snapshot dir (`.orphaned-N` suffix) must not
    write a tombstone under the original (no-suffix) loop_id at all. Doing so would tombstone
    that loop_id from inside a coord lock keyed on the snapshot's own (different) directory
    name - never holding the original loop_id's own coord lock - racing a concurrently
    resumed/re-spawned run for the same Issue that legitimately does hold it. The snapshot
    directory itself is still deleted normally."""
    _init_repo(tmp_path)
    original_loop_id = "a-issue-1"
    _seed_state(tmp_path, original_loop_id, status_value="pending", updated_at=_iso(40))
    loop_dir = lc.loop_dir(original_loop_id, str(tmp_path))
    orphaned_name = f"{original_loop_id}{lc.ORPHANED_PENDING_MARKER}1"
    orphaned_dir = loop_dir.parent / orphaned_name
    loop_dir.rename(orphaned_dir)

    status.purge_loop(orphaned_name, str(tmp_path))

    assert not orphaned_dir.exists()
    tombstone_path = lc.loop_root(str(tmp_path)) / f"{original_loop_id}.tombstone.json"
    assert not tombstone_path.exists()


def test_collect_summaries_shows_tombstoned_loop_with_terminal_status(tmp_path: Path) -> None:
    """SN2: a purged loop must still show up in `list`, with its frozen-at-purge-time
    (terminal) status, instead of vanishing without a trace."""
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))

    status.purge_loop(loop_id, str(tmp_path))

    summaries = status.collect_summaries(str(tmp_path))

    assert len(summaries) == 1
    assert summaries[0].loop_id == loop_id
    assert summaries[0].status == "passed"


def test_main_purge_dry_run_reports_candidates_without_deleting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))

    exit_code = status.main(["purge", "--project", str(tmp_path), "--dry-run"])

    assert exit_code == 0
    assert loop_id in capsys.readouterr().err
    assert lc.loop_dir(loop_id, str(tmp_path)).is_dir()


def test_main_purge_without_dry_run_deletes_candidates(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))

    exit_code = status.main(["purge", "--project", str(tmp_path), "--yes"])

    assert exit_code == 0
    assert not lc.loop_dir(loop_id, str(tmp_path)).exists()


def test_main_purge_without_yes_aborts_and_keeps_candidates(tmp_path: Path) -> None:
    """#25: a real (non-dry-run) purge without `--yes` must not delete anything.

    stdin is not a tty under pytest, so the confirmation prompt auto-declines.
    """
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))

    exit_code = status.main(["purge", "--project", str(tmp_path)])

    assert exit_code == 1
    assert lc.loop_dir(loop_id, str(tmp_path)).is_dir()


def test_main_purge_respects_local_retention_override(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(10))
    override_dir = tmp_path / ".claude" / "config" / "loop-harness"
    override_dir.mkdir(parents=True)
    (override_dir / "loop-harness.local.yaml").write_text(
        "retention:\n  purge_after_days: 5\n", encoding="utf-8"
    )

    exit_code = status.main(["purge", "--project", str(tmp_path), "--yes"])

    assert exit_code == 0
    assert not lc.loop_dir(loop_id, str(tmp_path)).exists()


# --------------------------------------------------------------------------------------------
# RM2: untombstone
# --------------------------------------------------------------------------------------------


def test_main_untombstone_removes_tombstone_file(tmp_path: Path) -> None:
    """RM2: `untombstone --loop-id <id>` removes the tombstone so the Issue can be
    discovered/resumed as a fresh run again - the only prior recourse was deleting the
    `.tombstone.json` file on disk by hand."""
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))
    status.purge_loop(loop_id, str(tmp_path))
    tombstone_path = lc.loop_root(str(tmp_path)) / f"{loop_id}.tombstone.json"
    assert tombstone_path.is_file()

    exit_code = status.main(["untombstone", "--loop-id", loop_id, "--project", str(tmp_path)])

    assert exit_code == 0
    assert not tombstone_path.exists()


def test_main_untombstone_reports_failure_when_no_tombstone_exists(tmp_path: Path) -> None:
    _init_repo(tmp_path)

    exit_code = status.main(
        ["untombstone", "--loop-id", "no-such-loop", "--project", str(tmp_path)]
    )

    assert exit_code == 1


def test_main_untombstone_rejects_unsafe_loop_id(tmp_path: Path) -> None:
    """RM2: `--loop-id` is used to build a filesystem path directly, so a path-traversal-style
    value (e.g. containing `..` or a path separator) must be rejected rather than resolved."""
    _init_repo(tmp_path)

    exit_code = status.main(["untombstone", "--loop-id", "../escape", "--project", str(tmp_path)])

    assert exit_code == 1


def test_purge_if_still_safe_reloads_state_and_skips_now_running(tmp_path: Path) -> None:
    """#15: race guard — a loop that became `running` after candidate selection is spared."""
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))
    # Simulate the loop transitioning back to running in the window between candidate
    # collection and deletion (state.json is the source of truth `_purge_if_still_safe` re-reads).
    state = lc.load_state(loop_id, str(tmp_path))
    state.status = "running"
    lc._write_state(state, str(tmp_path))

    status._purge_if_still_safe(loop_id, str(tmp_path))

    assert lc.loop_dir(loop_id, str(tmp_path)).is_dir()


def test_purge_if_still_safe_deletes_when_status_unchanged(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))

    status._purge_if_still_safe(loop_id, str(tmp_path))

    assert not lc.loop_dir(loop_id, str(tmp_path)).exists()


def test_purge_loop_propagates_deletion_failure_as_loop_harness_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#14: a real removal failure must not be swallowed by `ignore_errors=True`."""
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(status.shutil, "rmtree", _boom)

    with pytest.raises(lc.LoopHarnessError):
        status.purge_loop(loop_id, str(tmp_path))


def test_purge_loop_is_a_noop_when_directory_already_gone(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))
    shutil.rmtree(lc.loop_dir(loop_id, str(tmp_path)))

    status.purge_loop(loop_id, str(tmp_path))  # must not raise


def test_purge_if_still_safe_holds_lock_flock_during_reload_and_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F19: the loop's lock-file flock must be held across the reload + purge window.

    A concurrent `acquire_lock`/`reacquire_lease` takes the same lock-file flock, so
    holding it here closes the TOCTOU window between the state reload and `purge_loop`.
    """
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))
    lc.acquire_lock(loop_id, str(tmp_path), "owner-1", ttl_seconds=60)

    lock_was_free_during_purge = []

    def _spy_purge_loop(loop_id_arg: str, project_dir_arg: str) -> None:
        lock_file = lc.lock_path(loop_id_arg, project_dir_arg)
        fd = os.open(lock_file, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_was_free_during_purge.append(True)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            lock_was_free_during_purge.append(False)
        finally:
            os.close(fd)

    monkeypatch.setattr(status, "purge_loop", _spy_purge_loop)

    status._purge_if_still_safe(loop_id, str(tmp_path))

    assert lock_was_free_during_purge == [False]


def test_purge_if_still_safe_holds_coord_lock_during_reload_and_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SN-flock: `_purge_if_still_safe` must hold the fixed, purge-independent coord lock
    (`lc.held_coord_lock`) across the whole reload+purge window - unlike the inner lock.json
    flock (F19, tested above), which stops protecting anything the instant `purge_loop`'s
    `rmtree` deletes that file's inode. A concurrent `resume`/`reacquire_lease` (which now
    also take this same coord lock, see `test_loop_common_lock.py`) must actually contend on
    this path, not race a purge in-flight."""
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))

    coord_lock_was_free_during_purge = []

    def _spy_purge_loop(loop_id_arg: str, project_dir_arg: str) -> None:
        coord_lock_file = lc.coord_lock_path(loop_id_arg, project_dir_arg)
        fd = os.open(coord_lock_file, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            coord_lock_was_free_during_purge.append(True)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            coord_lock_was_free_during_purge.append(False)
        finally:
            os.close(fd)

    monkeypatch.setattr(status, "purge_loop", _spy_purge_loop)

    status._purge_if_still_safe(loop_id, str(tmp_path))

    assert coord_lock_was_free_during_purge == [False]


def test_purge_if_still_safe_falls_back_when_lock_file_absent(tmp_path: Path) -> None:
    """F19/SH2(a): with no lock.json for the loop (the common case for a `failed` loop, whose
    lease was already released), a placeholder lock file is created under `O_CREAT | O_EXCL`
    purely to have a path to flock against a concurrent `resume()`/`reacquire_lease()`
    (SH2(b)); the safety check + purge still run as before."""
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))
    assert not lc.lock_path(loop_id, str(tmp_path)).exists()

    status._purge_if_still_safe(loop_id, str(tmp_path))

    assert not lc.loop_dir(loop_id, str(tmp_path)).exists()


def test_purge_if_still_safe_removes_placeholder_lock_when_purge_skipped(
    tmp_path: Path,
) -> None:
    """SH2(a): if the placeholder lock file created for a would-be lock-absent purge turns out
    to be unneeded (the reloaded state is no longer purge-eligible), it must be removed again
    so no stray lock.json is left behind for a loop this call did not actually purge."""
    _init_repo(tmp_path)
    loop_id = "a-issue-1"
    _seed_state(tmp_path, loop_id, status_value="passed", updated_at=_iso(31))
    assert not lc.lock_path(loop_id, str(tmp_path)).exists()
    # Simulate the loop transitioning back to running in the window between candidate
    # collection and this call (mirrors `test_purge_if_still_safe_reloads_state_and_skips_now_running`).
    state = lc.load_state(loop_id, str(tmp_path))
    state.status = "running"
    lc._write_state(state, str(tmp_path))

    status._purge_if_still_safe(loop_id, str(tmp_path))

    assert lc.loop_dir(loop_id, str(tmp_path)).is_dir()
    assert not lc.lock_path(loop_id, str(tmp_path)).exists()


def test_confirm_purge_accepts_yes_on_interactive_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """F22: isatty=True + input() == 'yes' -> confirmed."""
    monkeypatch.setattr(status.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    assert status._confirm_purge(3) is True


def test_confirm_purge_declines_on_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    """F18/F22: isatty=True + input() raising EOFError (Ctrl-D) -> declined, not propagated."""

    def _raise_eof(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(status.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", _raise_eof)

    assert status._confirm_purge(3) is False


def test_confirm_purge_declines_on_non_interactive_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """F22: isatty=False -> declined without ever calling input()."""

    def _fail_if_called(_prompt: str) -> str:
        raise AssertionError("input() must not be called when stdin is non-interactive")

    monkeypatch.setattr(status.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", _fail_if_called)

    assert status._confirm_purge(3) is False
