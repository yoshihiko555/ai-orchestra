"""Tests for the LP-2 resident scheduler (`loop_scheduler.py`).

Covers discovery (label resolution, priority/created_at ordering, active-loop exclusion),
the concurrency cap, restart-vs-terminal-status handling, startup repo-identity safety
stop, and cron/launchd template generation, per the evaluation set (EV-46, EV-48, EV-51,
EV-70) and the handoff's required coverage list. No real `gh`/`claude` process is ever
invoked: `gh api`/`gh repo view` are isolated behind single functions that tests
monkeypatch, and worker spawning is monkeypatched to fake `Popen`-like objects.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.module_loader import load_module

lc = load_module("loop_common", "packages/loop-harness/lib/loop_common.py")
ld = load_module("loop_definition", "packages/loop-harness/lib/loop_definition.py")
wm = load_module("worktree_manager", "packages/loop-harness/lib/worktree_manager.py")
lds = load_module("loop_driver_support", "packages/loop-harness/lib/loop_driver_support.py")
scheduler = load_module("loop_scheduler", "packages/loop-harness/scripts/loop_scheduler.py")


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
    status: str = "running",
    repo_identity_hash: str | None = None,
    phase: str = "implementation",
) -> lc.LoopState:
    """Write a minimal state.json for loop_id, returning the in-memory state used."""
    project_dir = str(tmp_path)
    repo_hash = repo_identity_hash or wm.resolve_repo_identity_hash(project_dir)
    state = lc._initial_state(loop_id, "issue-loop", repo_hash, project_dir, "main", phase)
    state.status = status
    lc._write_state(state, project_dir)
    return state


class _FakePopen:
    """Minimal stand-in for subprocess.Popen used to avoid spawning real processes."""

    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.spawned_for: str | None = None

    def poll(self) -> int | None:
        return self.returncode


# --------------------------------------------------------------------------------------------
# config resolution (packages/loop-harness/config/loop-harness.yaml)
# --------------------------------------------------------------------------------------------


def test_concurrency_limit_default(tmp_path: Path) -> None:
    assert scheduler.concurrency_limit(str(tmp_path)) == 2


def test_concurrency_limit_local_override(tmp_path: Path) -> None:
    override_dir = tmp_path / ".claude" / "config" / "loop-harness"
    override_dir.mkdir(parents=True)
    (override_dir / "loop-harness.local.yaml").write_text(
        "lp2:\n  concurrency_limit: 5\n", encoding="utf-8"
    )
    assert scheduler.concurrency_limit(str(tmp_path)) == 5


def test_priority_labels_default_empty(tmp_path: Path) -> None:
    assert scheduler.priority_labels(str(tmp_path)) == []


def test_priority_labels_local_override(tmp_path: Path) -> None:
    override_dir = tmp_path / ".claude" / "config" / "loop-harness"
    override_dir.mkdir(parents=True)
    (override_dir / "loop-harness.local.yaml").write_text(
        "lp2:\n  priority_labels:\n    - priority:high\n    - priority:medium\n",
        encoding="utf-8",
    )
    assert scheduler.priority_labels(str(tmp_path)) == ["priority:high", "priority:medium"]


# --------------------------------------------------------------------------------------------
# loop-definition-derived discovery parameters (3.1 節: never hardcode the label)
# --------------------------------------------------------------------------------------------


def test_resolve_label_from_bundled_issue_loop_definition(tmp_path: Path) -> None:
    definition = ld.load_all_definitions(str(tmp_path))["issue-loop"]
    assert scheduler.resolve_label(definition) == "loop:issue"


def test_resolve_poll_interval_from_bundled_issue_loop_definition(tmp_path: Path) -> None:
    definition = ld.load_all_definitions(str(tmp_path))["issue-loop"]
    assert scheduler.resolve_poll_interval(definition) == 300


def test_resolve_label_requires_trigger_lp2() -> None:
    definition = ld.LoopDefinition(id="x", trigger={}, phases=[], notifications={}, source_path="")
    with pytest.raises(ld.DefinitionValidationError):
        scheduler.resolve_label(definition)


def test_resolve_label_requires_label_key() -> None:
    definition = ld.LoopDefinition(
        id="x", trigger={"lp2": {}}, phases=[], notifications={}, source_path=""
    )
    with pytest.raises(ld.DefinitionValidationError):
        scheduler.resolve_label(definition)


def test_resolve_poll_interval_defaults_when_key_absent() -> None:
    definition = ld.LoopDefinition(
        id="x", trigger={"lp2": {"label": "loop:x"}}, phases=[], notifications={}, source_path=""
    )
    assert scheduler.resolve_poll_interval(definition) == scheduler.DEFAULT_POLL_INTERVAL_SECONDS


# --------------------------------------------------------------------------------------------
# sort_candidates: priority label rank, then created_at ascending
# --------------------------------------------------------------------------------------------


def test_sort_candidates_orders_by_priority_then_created_at() -> None:
    issues = [
        {"number": 1, "created_at": "2026-01-01T00:00:00Z", "labels": []},
        {"number": 2, "created_at": "2026-01-03T00:00:00Z", "labels": [{"name": "priority:high"}]},
        {"number": 3, "created_at": "2026-01-02T00:00:00Z", "labels": []},
    ]
    ordered = scheduler.sort_candidates(issues, ["priority:high"])
    assert [item["number"] for item in ordered] == [2, 1, 3]


def test_sort_candidates_plain_created_at_when_no_priority_labels_configured() -> None:
    issues = [
        {"number": 3, "created_at": "2026-01-03T00:00:00Z", "labels": []},
        {"number": 1, "created_at": "2026-01-01T00:00:00Z", "labels": []},
        {"number": 2, "created_at": "2026-01-02T00:00:00Z", "labels": []},
    ]
    ordered = scheduler.sort_candidates(issues, [])
    assert [item["number"] for item in ordered] == [1, 2, 3]


# --------------------------------------------------------------------------------------------
# active_loop_ids / discover_loop_ids (EV-70)
# --------------------------------------------------------------------------------------------


def test_active_loop_ids_filters_running_and_waiting_external(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed_state(tmp_path, "aaaaaaaa-issue-1", status="running")
    _seed_state(tmp_path, "aaaaaaaa-issue-2", status="waiting_external")
    _seed_state(tmp_path, "aaaaaaaa-issue-3", status="passed")
    active = scheduler.active_loop_ids(str(tmp_path))
    assert active == {"aaaaaaaa-issue-1", "aaaaaaaa-issue-2"}


def test_discover_loop_ids_excludes_active_and_orders_by_created_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    definition = ld.load_all_definitions(project_dir)["issue-loop"]

    issue_2_loop_id = wm.compute_loop_id(project_dir, 2)
    _seed_state(tmp_path, issue_2_loop_id, status="running")

    fake_issues = [
        {"number": 3, "created_at": "2026-01-02T00:00:00Z", "labels": []},
        {"number": 1, "created_at": "2026-01-01T00:00:00Z", "labels": []},
        {"number": 2, "created_at": "2026-01-01T00:00:01Z", "labels": []},
    ]
    monkeypatch.setattr(
        scheduler,
        "list_labeled_issues",
        lambda project, label: fake_issues if label == "loop:issue" else [],
    )

    loop_ids = scheduler.discover_loop_ids(project_dir, definition)

    assert loop_ids == [wm.compute_loop_id(project_dir, 1), wm.compute_loop_id(project_dir, 3)]
    assert issue_2_loop_id not in loop_ids


def test_discover_loop_ids_applies_configured_priority_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    definition = ld.load_all_definitions(project_dir)["issue-loop"]
    override_dir = tmp_path / ".claude" / "config" / "loop-harness"
    override_dir.mkdir(parents=True)
    (override_dir / "loop-harness.local.yaml").write_text(
        "lp2:\n  priority_labels:\n    - priority:high\n", encoding="utf-8"
    )

    fake_issues = [
        {"number": 1, "created_at": "2026-01-01T00:00:00Z", "labels": []},
        {"number": 3, "created_at": "2026-01-02T00:00:00Z", "labels": [{"name": "priority:high"}]},
    ]
    monkeypatch.setattr(scheduler, "list_labeled_issues", lambda project, label: fake_issues)

    loop_ids = scheduler.discover_loop_ids(project_dir, definition)

    assert loop_ids == [wm.compute_loop_id(project_dir, 3), wm.compute_loop_id(project_dir, 1)]


@pytest.mark.parametrize("status", ["passed", "failed", "stopped"])
def test_discover_loop_ids_excludes_terminal_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """A loop_id that already reached a terminal outcome must not be regenerated just because
    its Issue still carries the label (#10); resuming it is an explicit operator action."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    definition = ld.load_all_definitions(project_dir)["issue-loop"]

    terminal_loop_id = wm.compute_loop_id(project_dir, 5)
    _seed_state(tmp_path, terminal_loop_id, status=status)

    fake_issues = [{"number": 5, "created_at": "2026-01-01T00:00:00Z", "labels": []}]
    monkeypatch.setattr(scheduler, "list_labeled_issues", lambda project, label: fake_issues)

    assert scheduler.discover_loop_ids(project_dir, definition) == []


def test_discover_loop_ids_excludes_tombstoned_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SN2: a loop_id whose state dir was purged (leaving a tombstone) must not be regenerated
    just because its Issue still carries the label - same rationale as
    `test_discover_loop_ids_excludes_terminal_loops`, extended to the purged case where
    state.json no longer exists at all to report a terminal status."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    definition = ld.load_all_definitions(project_dir)["issue-loop"]

    purged_loop_id = wm.compute_loop_id(project_dir, 5)
    lc.loop_root(project_dir).mkdir(parents=True, exist_ok=True)
    tombstone_path = lc.loop_root(project_dir) / f"{purged_loop_id}.tombstone.json"
    tombstone_path.write_text(
        json.dumps({"loop_id": purged_loop_id, "status": "passed", "purged_at": lc.now_iso()}),
        encoding="utf-8",
    )

    fake_issues = [{"number": 5, "created_at": "2026-01-01T00:00:00Z", "labels": []}]
    monkeypatch.setattr(scheduler, "list_labeled_issues", lambda project, label: fake_issues)

    assert scheduler.discover_loop_ids(project_dir, definition) == []


def test_discover_loop_ids_excludes_pending_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loop_id still `pending` (initial run_maker never completed) must not be re-spawned as
    a "new" candidate either (#G10): `lc.attach()` rejects `pending`, so a duplicate worker
    would fail to attach every cycle - a restart storm. Excluding it from discovery leaves it
    for a human to investigate instead."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    definition = ld.load_all_definitions(project_dir)["issue-loop"]

    pending_loop_id = wm.compute_loop_id(project_dir, 6)
    _seed_state(tmp_path, pending_loop_id, status="pending")

    fake_issues = [{"number": 6, "created_at": "2026-01-01T00:00:00Z", "labels": []}]
    monkeypatch.setattr(scheduler, "list_labeled_issues", lambda project, label: fake_issues)

    assert scheduler.discover_loop_ids(project_dir, definition) == []


def test_discover_loop_ids_respects_in_memory_excluded_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    definition = ld.load_all_definitions(project_dir)["issue-loop"]
    fake_issues = [{"number": 7, "created_at": "2026-01-01T00:00:00Z", "labels": []}]
    monkeypatch.setattr(scheduler, "list_labeled_issues", lambda project, label: fake_issues)
    excluded = frozenset({wm.compute_loop_id(project_dir, 7)})
    assert scheduler.discover_loop_ids(project_dir, definition, excluded=excluded) == []


def test_list_labeled_issues_builds_expected_gh_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, tuple[str, str]] = {}

    def fake_repo_name(project_dir: str) -> str:
        return "acme/widgets"

    def fake_gh_list(repo: str, label: str, project_dir: str) -> str:
        captured["args"] = (repo, label)
        return json.dumps([{"number": 9, "created_at": "2026-01-01T00:00:00Z", "labels": []}])

    monkeypatch.setattr(scheduler, "_repo_name_with_owner", fake_repo_name)
    monkeypatch.setattr(scheduler, "_gh_list_issues", fake_gh_list)

    issues = scheduler.list_labeled_issues("/some/project", "loop:issue")

    assert captured["args"] == ("acme/widgets", "loop:issue")
    assert issues == [{"number": 9, "created_at": "2026-01-01T00:00:00Z", "labels": []}]


def test_list_labeled_issues_excludes_pull_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GET /repos/{repo}/issues` also returns PRs (they carry a `pull_request` key); those
    must not be treated as loop-discovery candidates (#9)."""
    monkeypatch.setattr(scheduler, "_repo_name_with_owner", lambda project_dir: "acme/widgets")
    monkeypatch.setattr(
        scheduler,
        "_gh_list_issues",
        lambda repo, label, project_dir: json.dumps(
            [
                {"number": 1, "created_at": "2026-01-01T00:00:00Z", "labels": []},
                {
                    "number": 2,
                    "created_at": "2026-01-02T00:00:00Z",
                    "labels": [],
                    "pull_request": {"url": "https://example/pulls/2"},
                },
            ]
        ),
    )

    issues = scheduler.list_labeled_issues("/some/project", "loop:issue")

    assert [item["number"] for item in issues] == [1]


def test_list_labeled_issues_parses_multiple_paginated_json_arrays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`gh api --paginate` writes one JSON array per page back-to-back with no separator and
    no top-level wrapping array; parsing must not silently truncate at page 1 (#9)."""
    monkeypatch.setattr(scheduler, "_repo_name_with_owner", lambda project_dir: "acme/widgets")
    page_1 = json.dumps([{"number": 1, "created_at": "2026-01-01T00:00:00Z", "labels": []}])
    page_2 = json.dumps([{"number": 2, "created_at": "2026-01-02T00:00:00Z", "labels": []}])
    monkeypatch.setattr(
        scheduler, "_gh_list_issues", lambda repo, label, project_dir: page_1 + page_2
    )

    issues = scheduler.list_labeled_issues("/some/project", "loop:issue")

    assert [item["number"] for item in issues] == [1, 2]


def test_list_labeled_issues_returns_empty_on_gh_failure_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed `gh` call must not raise and take the resident scheduler down with it (#34)."""
    monkeypatch.setattr(scheduler, "_repo_name_with_owner", lambda project_dir: "acme/widgets")
    monkeypatch.setattr(scheduler, "_gh_list_issues", lambda repo, label, project_dir: "")

    assert scheduler.list_labeled_issues("/some/project", "loop:issue") == []


def test_gh_list_issues_returns_empty_string_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_gh_list_issues` itself must use `check=False` and swallow a nonzero exit (#34)."""

    class _FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "gh: some transient API error\n"

    def fake_run(*args: object, **kwargs: object) -> _FakeCompleted:
        assert kwargs.get("check") is False
        assert kwargs.get("cwd") == "/some/project"
        return _FakeCompleted()

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)

    assert scheduler._gh_list_issues("acme/widgets", "loop:issue", "/some/project") == ""


def test_gh_list_issues_returns_empty_string_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `gh` call that exceeds the 30s timeout must not raise `TimeoutExpired` and take the
    resident scheduler down with it; it must honor the same "failure returns \"\"" contract as
    a nonzero exit (#F20)."""

    def fake_run(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="gh api ...", timeout=30)

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)

    assert scheduler._gh_list_issues("acme/widgets", "loop:issue", "/some/project") == ""


def test_gh_list_issues_returns_empty_string_on_missing_gh_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#H2: `gh` not being installed raises `FileNotFoundError` (an `OSError` subclass), not
    `TimeoutExpired`; without also catching `OSError` this would raise on every poll cycle
    instead of honoring the "failure returns \"\"" contract."""

    def fake_run(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("gh: command not found")

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)

    assert scheduler._gh_list_issues("acme/widgets", "loop:issue", "/some/project") == ""


# --------------------------------------------------------------------------------------------
# should_restart / reap_finished_workers (EV-51)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("running", True),
        ("waiting_external", True),
        ("pending", False),
        ("failed", False),
        ("stopped", False),
        ("passed", False),
    ],
)
def test_should_restart(status: str, expected: bool) -> None:
    """#H3/#H11: `pending` must not restart via this path (immediate attach() rejection would
    restart-storm); recovery for a genuinely orphaned `pending` loop is a separate mechanism
    (`recover_orphaned_pending_loops`), not a restart."""
    assert scheduler.should_restart(status) is expected


def test_reap_finished_workers_restarts_abnormal_exit_when_not_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-1"
    _seed_state(tmp_path, loop_id, status="running")
    runtime = scheduler.SchedulerRuntime(workers={loop_id: _FakePopen(returncode=1)})

    respawned = _FakePopen(returncode=None)
    monkeypatch.setattr(scheduler, "spawn_worker", lambda lid, project: respawned)

    result = scheduler.reap_finished_workers(runtime, project_dir)

    assert result == [loop_id]
    assert runtime.workers[loop_id] is respawned


def test_reap_finished_workers_does_not_restart_when_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-1"
    _seed_state(tmp_path, loop_id, status="stopped")
    runtime = scheduler.SchedulerRuntime(workers={loop_id: _FakePopen(returncode=1)})

    def _fail_spawn(lid: str, project: str) -> None:
        raise AssertionError("must not restart a safety-stopped loop")

    monkeypatch.setattr(scheduler, "spawn_worker", _fail_spawn)

    result = scheduler.reap_finished_workers(runtime, project_dir)

    assert result == []
    assert loop_id not in runtime.workers


def test_reap_finished_workers_does_not_restart_when_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-1"
    _seed_state(tmp_path, loop_id, status="failed")
    runtime = scheduler.SchedulerRuntime(workers={loop_id: _FakePopen(returncode=1)})

    def _fail_spawn(lid: str, project: str) -> None:
        raise AssertionError("must not restart a normally-failed loop")

    monkeypatch.setattr(scheduler, "spawn_worker", _fail_spawn)

    result = scheduler.reap_finished_workers(runtime, project_dir)

    assert result == []


def test_reap_finished_workers_does_not_restart_when_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G7: a loop that already reached `passed` must never be restarted, even if the worker
    process itself exits abnormally after writing the final status (e.g. killed mid-teardown).
    """
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-1"
    _seed_state(tmp_path, loop_id, status="passed")
    runtime = scheduler.SchedulerRuntime(workers={loop_id: _FakePopen(returncode=1)})

    def _fail_spawn(lid: str, project: str) -> None:
        raise AssertionError("must not restart an already-passed loop")

    monkeypatch.setattr(scheduler, "spawn_worker", _fail_spawn)

    result = scheduler.reap_finished_workers(runtime, project_dir)

    assert result == []
    assert loop_id not in runtime.workers


def test_reap_finished_workers_leaves_still_running_children_alone(tmp_path: Path) -> None:
    loop_id = "aaaaaaaa-issue-1"
    runtime = scheduler.SchedulerRuntime(workers={loop_id: _FakePopen(returncode=None)})
    result = scheduler.reap_finished_workers(runtime, str(tmp_path))
    assert result == []
    assert loop_id in runtime.workers


def test_reap_finished_workers_skips_clean_exit_without_restart(tmp_path: Path) -> None:
    loop_id = "aaaaaaaa-issue-1"
    runtime = scheduler.SchedulerRuntime(workers={loop_id: _FakePopen(returncode=0)})
    result = scheduler.reap_finished_workers(runtime, str(tmp_path))
    assert result == []
    assert loop_id not in runtime.workers


# --------------------------------------------------------------------------------------------
# reap_finished_workers: foreign-lease restart-storm cooldown (code H4)
# --------------------------------------------------------------------------------------------


def test_reap_finished_workers_does_not_immediately_respawn_foreign_lease_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that foreign-lease-exits (returncode 3) never reaches `LoopDriver`, so
    `state.json.status` stays "running" (owned by the foreign process). Respawning it every
    cycle would restart-storm until the foreign lease's own TTL naturally expires."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-1"
    _seed_state(tmp_path, loop_id, status="running")
    runtime = scheduler.SchedulerRuntime(
        workers={loop_id: _FakePopen(returncode=scheduler._EXIT_FOREIGN_LEASE)}
    )

    def _fail_spawn(lid: str, project: str) -> None:
        raise AssertionError("must not respawn a foreign-lease-rejected worker immediately")

    monkeypatch.setattr(scheduler, "spawn_worker", _fail_spawn)

    result = scheduler.reap_finished_workers(runtime, project_dir)

    assert result == []
    assert loop_id not in runtime.workers
    assert loop_id in runtime.foreign_lease_cooldown_until


def test_reap_finished_workers_respawns_after_foreign_lease_cooldown_elapses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """code H4: once the cooldown window elapses, the loop_id becomes eligible again."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-1"
    _seed_state(tmp_path, loop_id, status="running")
    runtime = scheduler.SchedulerRuntime(
        workers={loop_id: _FakePopen(returncode=scheduler._EXIT_FOREIGN_LEASE)}
    )

    fake_clock = {"now": 1000.0}
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: fake_clock["now"])
    monkeypatch.setattr(scheduler, "lp2_lease_ttl_seconds", lambda _project: 300)

    first = scheduler.reap_finished_workers(runtime, project_dir)
    assert first == []
    assert loop_id not in runtime.workers

    # Still within the cooldown window: no respawn yet.
    fake_clock["now"] += 100
    respawned = _FakePopen(returncode=None)
    monkeypatch.setattr(scheduler, "spawn_worker", lambda lid, project: respawned)
    still_cooling = scheduler.reap_finished_workers(runtime, project_dir)
    assert still_cooling == []
    assert loop_id not in runtime.workers

    # Past the cooldown window: eligible for restart again.
    fake_clock["now"] += 300
    after_cooldown = scheduler.reap_finished_workers(runtime, project_dir)
    assert after_cooldown == [loop_id]
    assert runtime.workers[loop_id] is respawned
    assert loop_id not in runtime.foreign_lease_cooldown_until


def test_reap_finished_workers_does_not_respawn_cooldown_when_at_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#11: an expired cooldown must respect the concurrency cap, not bypass it. Left in
    `foreign_lease_cooldown_until` so it stays excluded from fresh discovery too."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    cooling_loop_id = "aaaaaaaa-issue-1"
    _seed_state(tmp_path, cooling_loop_id, status="running")
    # Fill both concurrency_limit(default 2) slots with unrelated already-running workers.
    runtime = scheduler.SchedulerRuntime(
        workers={"busy-1": _FakePopen(None), "busy-2": _FakePopen(None)},
        foreign_lease_cooldown_until={cooling_loop_id: 500.0},
    )
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: 1000.0)

    def _fail_spawn(lid: str, project: str) -> None:
        raise AssertionError("must not respawn past the concurrency cap")

    monkeypatch.setattr(scheduler, "spawn_worker", _fail_spawn)

    result = scheduler.reap_finished_workers(runtime, project_dir)

    assert result == []
    assert cooling_loop_id not in runtime.workers
    assert cooling_loop_id in runtime.foreign_lease_cooldown_until


def test_reap_finished_workers_counts_untracked_live_loop_against_cooldown_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SN1: an untracked-but-live active loop (left running by a previous scheduler process
    across a restart, not tracked in `runtime.workers`) must count against the concurrency cap
    for the cooldown-respawn path too - previously only `spawn_new_workers` counted it
    (`_untracked_live_active_loop_ids`), so this path could respawn a cooldown-elapsed loop
    past the configured limit whenever an untracked-live loop was occupying a slot."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    override_dir = tmp_path / ".claude" / "config" / "loop-harness"
    override_dir.mkdir(parents=True)
    (override_dir / "loop-harness.local.yaml").write_text(
        "lp2:\n  concurrency_limit: 1\n", encoding="utf-8"
    )
    occupying_loop_id = "aaaaaaaa-issue-1"
    cooldown_loop_id = "aaaaaaaa-issue-2"
    _seed_state(tmp_path, occupying_loop_id, status="running")
    lc.acquire_lock(occupying_loop_id, project_dir, "previous-process", 300)  # still alive
    _seed_state(tmp_path, cooldown_loop_id, status="running")  # foreign lease already expired
    runtime = scheduler.SchedulerRuntime(foreign_lease_cooldown_until={cooldown_loop_id: 500.0})
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: 1000.0)

    def _fail_spawn(lid: str, project: str) -> None:
        raise AssertionError("must not respawn past the concurrency cap")

    monkeypatch.setattr(scheduler, "spawn_worker", _fail_spawn)

    result = scheduler.reap_finished_workers(runtime, project_dir)

    assert result == []
    assert cooldown_loop_id not in runtime.workers
    assert cooldown_loop_id in runtime.foreign_lease_cooldown_until


def test_reap_finished_workers_respawns_cooldown_using_slot_freed_this_same_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SN6: a cooldown-elapsed loop must be able to use a concurrency slot freed by reaping a
    dead worker within this *same* `reap_finished_workers` call, not only on a later cycle.
    Checking cooldown-expiry before reaping would compute headroom against the stale
    (pre-reap) worker count and starve the cooldown-elapsed loop for a full extra cycle."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    cooling_loop_id = "aaaaaaaa-issue-1"
    finishing_loop_id = "aaaaaaaa-issue-2"
    _seed_state(tmp_path, cooling_loop_id, status="running")
    # Both concurrency_limit(default 2) slots are occupied at call-start: one still-running
    # unrelated worker, and one that has just exited cleanly (returncode 0) and is about to be
    # reaped by this same call.
    runtime = scheduler.SchedulerRuntime(
        workers={"busy-1": _FakePopen(None), finishing_loop_id: _FakePopen(returncode=0)},
        foreign_lease_cooldown_until={cooling_loop_id: 500.0},
    )
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: 1000.0)  # cooldown already elapsed

    respawned_proc = _FakePopen(returncode=None)
    monkeypatch.setattr(scheduler, "spawn_worker", lambda lid, project: respawned_proc)

    result = scheduler.reap_finished_workers(runtime, project_dir)

    assert cooling_loop_id in result
    assert runtime.workers[cooling_loop_id] is respawned_proc
    assert cooling_loop_id not in runtime.foreign_lease_cooldown_until
    assert finishing_loop_id not in runtime.workers


# --------------------------------------------------------------------------------------------
# reap_finished_workers: immediate-restart branch must respect the concurrency cap (RH4)
# --------------------------------------------------------------------------------------------


def test_reap_finished_workers_immediate_restart_respects_cap_with_untracked_live_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RH4: the reap-and-immediately-restart branch for an abnormally-exited, non-terminal
    worker previously spawned unconditionally the moment `should_restart(state.status)` was
    true, without ever consulting `_available_worker_slots`. With cap=2 (default), one dead
    worker + one still-live tracked worker + one untracked-but-live active loop (SN1; e.g. left
    running by a previous scheduler process across a restart) already fully occupies the cap -
    immediately restarting the dead worker on top of that would run 3 workers concurrently,
    past the configured limit."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    dead_loop_id = "aaaaaaaa-issue-1"
    live_tracked_loop_id = "aaaaaaaa-issue-2"
    untracked_live_loop_id = "aaaaaaaa-issue-3"
    _seed_state(tmp_path, dead_loop_id, status="running")
    _seed_state(tmp_path, live_tracked_loop_id, status="running")
    _seed_state(tmp_path, untracked_live_loop_id, status="running")
    lc.acquire_lock(untracked_live_loop_id, project_dir, "previous-process", 300)  # still alive
    runtime = scheduler.SchedulerRuntime(
        workers={
            dead_loop_id: _FakePopen(returncode=1),
            live_tracked_loop_id: _FakePopen(returncode=None),
        }
    )

    def _fail_spawn(lid: str, project: str) -> None:
        raise AssertionError("must not restart past the concurrency cap")

    monkeypatch.setattr(scheduler, "spawn_worker", _fail_spawn)

    result = scheduler.reap_finished_workers(runtime, project_dir)

    assert result == []
    assert dead_loop_id not in runtime.workers


def test_reap_finished_workers_immediate_restart_uses_slot_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RH4 regression guard: the slot-aware immediate-restart path must still actually restart
    a dead worker when a slot genuinely is free (cap not yet reached) - the fix must not
    degenerate into "never immediately restart"."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    dead_loop_id = "aaaaaaaa-issue-1"
    untracked_live_loop_id = "aaaaaaaa-issue-2"
    _seed_state(tmp_path, dead_loop_id, status="running")
    _seed_state(tmp_path, untracked_live_loop_id, status="running")
    lc.acquire_lock(untracked_live_loop_id, project_dir, "previous-process", 300)  # still alive
    runtime = scheduler.SchedulerRuntime(workers={dead_loop_id: _FakePopen(returncode=1)})

    respawned = _FakePopen(returncode=None)
    monkeypatch.setattr(scheduler, "spawn_worker", lambda lid, project: respawned)

    result = scheduler.reap_finished_workers(runtime, project_dir)

    assert result == [dead_loop_id]
    assert runtime.workers[dead_loop_id] is respawned


# --------------------------------------------------------------------------------------------
# respawn_orphaned_active_loops: scheduler-restart recovery (#F4)
# --------------------------------------------------------------------------------------------


def test_respawn_orphaned_active_loops_respawns_when_lease_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a scheduler restart, `runtime.workers` starts empty even though a previous
    process left this loop running; with no lock file at all there is no live owner, so the
    loop must be respawned rather than permanently stranded."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-1"
    _seed_state(tmp_path, loop_id, status="running")
    runtime = scheduler.SchedulerRuntime()

    respawned_proc = _FakePopen(returncode=None)
    monkeypatch.setattr(scheduler, "spawn_worker", lambda lid, project: respawned_proc)

    result = scheduler.respawn_orphaned_active_loops(runtime, project_dir)

    assert result == [loop_id]
    assert runtime.workers[loop_id] is respawned_proc


def test_respawn_orphaned_active_loops_skips_when_lease_still_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live lease means some other owner (another host/process, or this process's own
    earlier-cycle child) still holds the loop; respawning would create a duplicate worker."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-1"
    _seed_state(tmp_path, loop_id, status="running")
    lc.acquire_lock(loop_id, project_dir, "someone-else", 300)
    runtime = scheduler.SchedulerRuntime()

    def _fail_spawn(lid: str, project: str) -> None:
        raise AssertionError("must not respawn a loop with a live foreign lease")

    monkeypatch.setattr(scheduler, "spawn_worker", _fail_spawn)

    result = scheduler.respawn_orphaned_active_loops(runtime, project_dir)

    assert result == []
    assert loop_id not in runtime.workers


@pytest.mark.parametrize("status", ["stopped", "failed"])
def test_respawn_orphaned_active_loops_never_touches_terminal_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """EV-51: a safety-stopped (or normally-failed) loop must never be auto-restarted, even
    with no lease at all."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-1"
    _seed_state(tmp_path, loop_id, status=status)
    runtime = scheduler.SchedulerRuntime()

    def _fail_spawn(lid: str, project: str) -> None:
        raise AssertionError(f"must not respawn a {status} loop")

    monkeypatch.setattr(scheduler, "spawn_worker", _fail_spawn)

    result = scheduler.respawn_orphaned_active_loops(runtime, project_dir)

    assert result == []


def test_respawn_orphaned_active_loops_respects_concurrency_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple orphaned active loops must still respect the concurrency cap; only the
    available slots get spawned this cycle."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id_1 = "aaaaaaaa-issue-1"
    loop_id_2 = "aaaaaaaa-issue-2"
    _seed_state(tmp_path, loop_id_1, status="running")
    _seed_state(tmp_path, loop_id_2, status="waiting_external")
    # Default concurrency_limit is 2; occupy one slot with an unrelated worker so only one of
    # the two orphaned loops can be respawned this cycle.
    runtime = scheduler.SchedulerRuntime(workers={"busy-1": _FakePopen(None)})

    spawned_ids: list[str] = []

    def fake_spawn(lid: str, project: str) -> _FakePopen:
        spawned_ids.append(lid)
        return _FakePopen(returncode=None)

    monkeypatch.setattr(scheduler, "spawn_worker", fake_spawn)

    result = scheduler.respawn_orphaned_active_loops(runtime, project_dir)

    assert len(result) == 1
    assert spawned_ids == result
    assert len(runtime.workers) == 2


def test_respawn_orphaned_active_loops_counts_untracked_live_loop_against_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SN1: an untracked-but-live active loop (its lease still held by another owner, not
    tracked in `runtime.workers`) must count against the concurrency cap here too - previously
    only `spawn_new_workers` counted it, so this path could respawn a second, lease-expired
    orphaned loop past the configured limit whenever an untracked-live loop occupied a slot."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    override_dir = tmp_path / ".claude" / "config" / "loop-harness"
    override_dir.mkdir(parents=True)
    (override_dir / "loop-harness.local.yaml").write_text(
        "lp2:\n  concurrency_limit: 1\n", encoding="utf-8"
    )
    live_loop_id = "aaaaaaaa-issue-1"
    expired_loop_id = "aaaaaaaa-issue-2"
    _seed_state(tmp_path, live_loop_id, status="running")
    _seed_state(tmp_path, expired_loop_id, status="running")
    lc.acquire_lock(live_loop_id, project_dir, "previous-process", 300)  # still alive
    runtime = scheduler.SchedulerRuntime()  # fresh restart; workers empty

    def _fail_spawn(lid: str, project: str) -> None:
        raise AssertionError("must not respawn past the concurrency cap")

    monkeypatch.setattr(scheduler, "spawn_worker", _fail_spawn)

    result = scheduler.respawn_orphaned_active_loops(runtime, project_dir)

    assert result == []
    assert expired_loop_id not in runtime.workers


# --------------------------------------------------------------------------------------------
# recover_orphaned_pending_loops (#H3/#H11)
# --------------------------------------------------------------------------------------------


def test_recover_orphaned_pending_loops_retires_dir_when_lease_expired(
    tmp_path: Path,
) -> None:
    """A `pending` loop with no live lease at all (no lock file) must be retired: renamed aside
    so the next discovery cycle treats its Issue as a brand-new candidate."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-9"
    _seed_state(tmp_path, loop_id, status="pending")
    runtime = scheduler.SchedulerRuntime()
    loop_dir = Path(project_dir) / ".claude" / "loop" / loop_id

    result = scheduler.recover_orphaned_pending_loops(runtime, project_dir)

    assert result == [loop_id]
    assert not loop_dir.exists()
    assert (loop_dir.parent / f"{loop_id}.orphaned-1").is_dir()


def test_recover_orphaned_pending_loops_removes_worktree_with_uncommitted_changes(
    tmp_path: Path,
) -> None:
    """#I7: retiring an orphaned-pending loop must also clean up its worktree - otherwise a
    dead Maker's uncommitted edits would silently carry over into the fresh run this retirement
    is meant to enable, since `worktree_manager.create_worktree` reuses an existing worktree
    on the expected branch as-is."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    issue_number = 9
    loop_id = f"aaaaaaaa-issue-{issue_number}"
    _seed_state(tmp_path, loop_id, status="pending")
    worktree = wm.create_worktree(project_dir, issue_number)
    dirty_path = Path(worktree.path) / "dead-maker-leftover.txt"
    dirty_path.write_text("uncommitted edit from a dead Maker\n", encoding="utf-8")
    assert dirty_path.exists()
    runtime = scheduler.SchedulerRuntime()
    loop_dir = Path(project_dir) / ".claude" / "loop" / loop_id

    result = scheduler.recover_orphaned_pending_loops(runtime, project_dir)

    assert result == [loop_id]
    assert not loop_dir.exists()
    assert not Path(worktree.path).exists()


def test_recover_orphaned_pending_loops_warns_but_retires_on_worktree_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#I7: worktree cleanup is best-effort - a `git worktree remove` failure must only be
    warned about, never block retiring the state dir (that rename is what actually frees the
    Issue for rediscovery)."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    issue_number = 9
    loop_id = f"aaaaaaaa-issue-{issue_number}"
    _seed_state(tmp_path, loop_id, status="pending")
    wm.create_worktree(project_dir, issue_number)
    runtime = scheduler.SchedulerRuntime()
    loop_dir = Path(project_dir) / ".claude" / "loop" / loop_id

    def _fail_remove(project: str, number: int, force: bool = False) -> None:
        raise wm.WorktreeError("boom")

    monkeypatch.setattr(wm, "remove_worktree", _fail_remove)

    result = scheduler.recover_orphaned_pending_loops(runtime, project_dir)

    assert result == [loop_id]
    assert not loop_dir.exists()
    assert (loop_dir.parent / f"{loop_id}.orphaned-1").is_dir()
    assert "boom" in capsys.readouterr().err


def test_recover_orphaned_pending_loops_skips_when_lease_still_alive(
    tmp_path: Path,
) -> None:
    """A live lease means some owner might still complete the pending loop; must not touch it."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-9"
    _seed_state(tmp_path, loop_id, status="pending")
    lc.acquire_lock(loop_id, project_dir, "someone-else", 300)
    runtime = scheduler.SchedulerRuntime()
    loop_dir = Path(project_dir) / ".claude" / "loop" / loop_id

    result = scheduler.recover_orphaned_pending_loops(runtime, project_dir)

    assert result == []
    assert loop_dir.is_dir()


def test_recover_orphaned_pending_loops_skips_loop_tracked_in_runtime_workers(
    tmp_path: Path,
) -> None:
    """A loop this process's own runtime is still tracking (its worker just has not written
    past `pending` yet) must not be treated as orphaned."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-9"
    _seed_state(tmp_path, loop_id, status="pending")
    runtime = scheduler.SchedulerRuntime(workers={loop_id: _FakePopen(None)})
    loop_dir = Path(project_dir) / ".claude" / "loop" / loop_id

    result = scheduler.recover_orphaned_pending_loops(runtime, project_dir)

    assert result == []
    assert loop_dir.is_dir()


def test_recover_orphaned_pending_loops_ignores_non_pending_status(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-9"
    _seed_state(tmp_path, loop_id, status="running")
    runtime = scheduler.SchedulerRuntime()

    result = scheduler.recover_orphaned_pending_loops(runtime, project_dir)

    assert result == []


def test_recover_orphaned_pending_loops_ignores_already_retired_dirs(
    tmp_path: Path,
) -> None:
    """A dir previously renamed aside (`.orphaned-N`) must not be re-processed forever."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-9"
    _seed_state(tmp_path, loop_id, status="pending")
    loop_dir = Path(project_dir) / ".claude" / "loop" / loop_id
    loop_dir.rename(loop_dir.parent / f"{loop_id}.orphaned-1")
    runtime = scheduler.SchedulerRuntime()

    result = scheduler.recover_orphaned_pending_loops(runtime, project_dir)

    assert result == []
    assert (loop_dir.parent / f"{loop_id}.orphaned-1").is_dir()


def test_recover_orphaned_pending_loops_picks_next_free_suffix(
    tmp_path: Path,
) -> None:
    """If `<loop_id>.orphaned-1` already exists (e.g. a prior recovery of a same-named retry),
    the next free suffix is used instead of overwriting it."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "aaaaaaaa-issue-9"
    _seed_state(tmp_path, loop_id, status="pending")
    loop_dir = Path(project_dir) / ".claude" / "loop" / loop_id
    existing_orphan = loop_dir.parent / f"{loop_id}.orphaned-1"
    existing_orphan.mkdir(parents=True)
    runtime = scheduler.SchedulerRuntime()

    result = scheduler.recover_orphaned_pending_loops(runtime, project_dir)

    assert result == [loop_id]
    assert existing_orphan.is_dir()
    assert (loop_dir.parent / f"{loop_id}.orphaned-2").is_dir()


def test_run_cycle_recovers_orphaned_pending_before_spawning_new_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#H3/#H11: recovery must happen before `spawn_new_workers` within the same cycle so the
    freed Issue is picked up immediately, not on a follow-up poll cycle."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    definition = ld.load_all_definitions(project_dir)["issue-loop"]
    loop_id = wm.compute_loop_id(project_dir, 42)
    _seed_state(tmp_path, loop_id, status="pending")

    fake_issues = [{"number": 42, "created_at": "2026-01-01T00:00:00Z", "labels": []}]
    monkeypatch.setattr(scheduler, "list_labeled_issues", lambda project, label: fake_issues)
    spawned: list[str] = []

    def fake_spawn(lid: str, project: str, definition_id: str = "issue-loop") -> _FakePopen:
        spawned.append(lid)
        return _FakePopen(returncode=None)

    monkeypatch.setattr(scheduler, "spawn_worker", fake_spawn)

    runtime = scheduler.SchedulerRuntime()
    scheduler.run_cycle(runtime, project_dir, definition)

    loop_dir = Path(project_dir) / ".claude" / "loop" / loop_id
    assert spawned == [loop_id]
    assert not loop_dir.exists()
    assert (loop_dir.parent / f"{loop_id}.orphaned-1").is_dir()


def test_spawn_new_workers_excludes_ids_still_in_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#11: a loop_id mid-cooldown must not be spawned as a "new" discovery candidate even
    though it holds no worker slot and is not in `stopped_loop_ids`."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    definition = ld.load_all_definitions(project_dir)["issue-loop"]
    cooling_loop_id = "aaaaaaaa-issue-1"
    runtime = scheduler.SchedulerRuntime(foreign_lease_cooldown_until={cooling_loop_id: 999.0})

    captured_excluded: dict[str, frozenset[str]] = {}

    def fake_discover(
        project: str, defn: object, *, excluded: frozenset[str] = frozenset()
    ) -> list[str]:
        captured_excluded["excluded"] = excluded
        return [lid for lid in ["a", cooling_loop_id] if lid not in excluded]

    monkeypatch.setattr(scheduler, "discover_loop_ids", fake_discover)
    monkeypatch.setattr(
        scheduler, "spawn_worker", lambda loop_id, project, definition_id: _FakePopen(None)
    )

    spawned = scheduler.spawn_new_workers(runtime, project_dir, definition)

    assert cooling_loop_id in captured_excluded["excluded"]
    assert spawned == ["a"]


# --------------------------------------------------------------------------------------------
# spawn_new_workers: concurrency cap (EV-46)
# --------------------------------------------------------------------------------------------


def test_spawn_new_workers_does_not_spawn_or_discover_when_cap_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    definition = ld.load_all_definitions(project_dir)["issue-loop"]
    runtime = scheduler.SchedulerRuntime(
        workers={"a": _FakePopen(None), "b": _FakePopen(None)}
    )  # cap is 2 by default

    def _fail_discover(*args: object, **kwargs: object) -> None:
        raise AssertionError("discovery must not run while at capacity")

    monkeypatch.setattr(scheduler, "discover_loop_ids", _fail_discover)

    spawned = scheduler.spawn_new_workers(runtime, project_dir, definition)

    assert spawned == []
    assert len(runtime.workers) == 2


def test_spawn_new_workers_counts_untracked_live_active_loops_against_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SN1: after a scheduler restart, active (running/waiting_external) loops left behind by
    the previous process are untracked in a fresh `SchedulerRuntime.workers` even though their
    lease is still alive (so `respawn_orphaned_active_loops` correctly leaves them alone). They
    must still count against the concurrency cap - otherwise `spawn_new_workers` would spawn
    brand-new workers on top of them and exceed the configured limit."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    definition = ld.load_all_definitions(project_dir)["issue-loop"]
    loop_id_1 = "aaaaaaaa-issue-1"
    loop_id_2 = "aaaaaaaa-issue-2"
    _seed_state(tmp_path, loop_id_1, status="running")
    _seed_state(tmp_path, loop_id_2, status="waiting_external")
    lc.acquire_lock(loop_id_1, project_dir, "previous-process", 300)
    lc.acquire_lock(loop_id_2, project_dir, "previous-process", 300)
    runtime = scheduler.SchedulerRuntime()  # cap is 2 by default; workers empty (fresh restart)

    def _fail_discover(*args: object, **kwargs: object) -> None:
        raise AssertionError("discovery must not run while at capacity")

    monkeypatch.setattr(scheduler, "discover_loop_ids", _fail_discover)

    spawned = scheduler.spawn_new_workers(runtime, project_dir, definition)

    assert spawned == []
    assert runtime.workers == {}


def test_spawn_new_workers_counts_live_pending_lease_against_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#I2: after a scheduler restart, a `pending` loop with a live lease (its worker's first
    `run_maker` is still in flight, left behind by the previous scheduler process) is untracked
    in a fresh `SchedulerRuntime.workers`. `_pending_loop_ids` only stops `discover_loop_ids`
    from re-spawning a duplicate for the *same* Issue (#G10); it must also count against the
    concurrency cap for *other* Issues, or the scheduler would spawn brand-new workers on top of
    it and exceed the configured limit."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    definition = ld.load_all_definitions(project_dir)["issue-loop"]
    override_dir = tmp_path / ".claude" / "config" / "loop-harness"
    override_dir.mkdir(parents=True)
    (override_dir / "loop-harness.local.yaml").write_text(
        "lp2:\n  concurrency_limit: 2\n", encoding="utf-8"
    )
    pending_loop_id = "aaaaaaaa-issue-1"
    _seed_state(tmp_path, pending_loop_id, status="pending")
    lc.acquire_lock(pending_loop_id, project_dir, "previous-process", 300)  # still alive
    runtime = scheduler.SchedulerRuntime()  # fresh restart: workers empty, 1 slot already taken

    monkeypatch.setattr(scheduler, "discover_loop_ids", lambda project, defn, **kw: ["b", "c"])
    monkeypatch.setattr(
        scheduler, "spawn_worker", lambda loop_id, project, definition_id: _FakePopen(None)
    )

    spawned = scheduler.spawn_new_workers(runtime, project_dir, definition)

    assert spawned == ["b"]
    assert set(runtime.workers) == {"b"}


def test_spawn_new_workers_spawns_only_up_to_available_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    definition = ld.load_all_definitions(project_dir)["issue-loop"]
    runtime = scheduler.SchedulerRuntime()  # cap 2, 0 running -> 2 slots available

    monkeypatch.setattr(scheduler, "discover_loop_ids", lambda project, defn, **kw: ["a", "b", "c"])
    monkeypatch.setattr(
        scheduler, "spawn_worker", lambda loop_id, project, definition_id: _FakePopen(None)
    )

    spawned = scheduler.spawn_new_workers(runtime, project_dir, definition)

    assert spawned == ["a", "b"]
    assert set(runtime.workers) == {"a", "b"}


def test_spawn_new_workers_passes_definition_id_to_spawn_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#G3: the definition actually used for discovery must be forwarded to `spawn_worker` so
    a brand-new loop's `loop_driver.py` child does not silently fall back to
    `DEFAULT_DEFINITION_ID` when the scheduler was started with a custom `--definition`."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    definition = ld.LoopDefinition(
        id="custom-loop",
        trigger={"lp2": {"label": "loop:custom"}},
        phases=[],
        notifications={},
        source_path="",
    )
    runtime = scheduler.SchedulerRuntime()
    monkeypatch.setattr(scheduler, "discover_loop_ids", lambda project, defn, **kw: ["a"])

    captured: dict[str, object] = {}

    def fake_spawn(loop_id: str, project: str, definition_id: str) -> _FakePopen:
        captured["definition_id"] = definition_id
        return _FakePopen(None)

    monkeypatch.setattr(scheduler, "spawn_worker", fake_spawn)

    spawned = scheduler.spawn_new_workers(runtime, project_dir, definition)

    assert spawned == ["a"]
    assert captured["definition_id"] == "custom-loop"


# --------------------------------------------------------------------------------------------
# verify_repo_identity_at_startup (EV-48)
# --------------------------------------------------------------------------------------------


def test_verify_repo_identity_at_startup_stops_mismatch_and_notifies_without_issue_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "deadbeef-issue-1"
    _seed_state(tmp_path, loop_id, status="running", repo_identity_hash="deadbeef")

    notified: list[str] = []
    monkeypatch.setattr(
        lds, "notify_macos", lambda title, message: notified.append(message) or True
    )
    monkeypatch.setattr(scheduler, "lds", lds)

    def _fail_comment(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not post an Issue comment on repo-identity mismatch")

    monkeypatch.setattr(lds, "post_issue_comment", _fail_comment)

    stopped = scheduler.verify_repo_identity_at_startup(project_dir)

    assert stopped == [loop_id]
    state = lc.load_state(loop_id, project_dir)
    assert state.status == "stopped"
    assert state.stop_reason == "repo_identity_mismatch"
    assert any("repo_identity_mismatch" in message for message in notified)

    journal = lc.journal_path(loop_id, project_dir).read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in journal]
    assert any(
        event["event"] == "stopped" and event["payload"]["stop_reason"] == "repo_identity_mismatch"
        for event in events
    )


def test_verify_repo_identity_at_startup_ignores_matching_hash(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    matching_hash = wm.resolve_repo_identity_hash(project_dir)
    loop_id = f"{matching_hash}-issue-1"
    _seed_state(tmp_path, loop_id, status="running", repo_identity_hash=matching_hash)

    stopped = scheduler.verify_repo_identity_at_startup(project_dir)

    assert stopped == []
    assert lc.load_state(loop_id, project_dir).status == "running"


def test_verify_repo_identity_at_startup_skips_already_terminal_loops(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "deadbeef-issue-1"
    _seed_state(tmp_path, loop_id, status="failed", repo_identity_hash="deadbeef")

    stopped = scheduler.verify_repo_identity_at_startup(project_dir)

    assert stopped == []
    assert lc.load_state(loop_id, project_dir).status == "failed"


def test_verify_repo_identity_at_startup_ignores_orphaned_pending_dirs(tmp_path: Path) -> None:
    """SH1: a retired orphaned-pending dir (`.orphaned-N` suffix, see
    `recover_orphaned_pending_loops`) still carries the *original* (no-suffix) `loop_id`
    inside its frozen state.json, with a mismatching `repo_identity_hash` (seeded here as
    `deadbeef` vs. this repo's real hash). Without filtering it out, `_safe_stop_repo_identity_
    mismatch` would write a brand-new `stopped` state.json at the *original* loop_dir path
    (which no longer exists after retirement) - permanently blocking that Issue from being
    rediscovered, while the orphaned dir itself is left untouched (still `pending`, never
    cleaned up)."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "deadbeef-issue-1"
    _seed_state(tmp_path, loop_id, status="pending", repo_identity_hash="deadbeef")
    loop_dir = Path(project_dir) / ".claude" / "loop" / loop_id
    orphaned_dir = loop_dir.parent / f"{loop_id}.orphaned-1"
    loop_dir.rename(orphaned_dir)

    stopped = scheduler.verify_repo_identity_at_startup(project_dir)

    assert stopped == []
    # The original (no-suffix) path must not be resurrected with a fresh `stopped` state.
    assert not loop_dir.exists()
    # The orphaned dir itself must be left completely untouched.
    orphaned_state = lc.load_state(orphaned_dir.name, project_dir)
    assert orphaned_state.status == "pending"


def test_verify_repo_identity_at_startup_skips_write_when_lease_still_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SN8: `_safe_stop_repo_identity_mismatch` holds no lease of its own, so an unfenced
    `stopped` write would race a live detached worker's own next in-flight persist and be
    silently overwritten - making the safety-stop a no-op in practice. While the lease is
    still alive the write must be skipped entirely (not just eventually overwritten), and
    the mismatched loop_id must not be reported as stopped."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "deadbeef-issue-1"
    _seed_state(tmp_path, loop_id, status="running", repo_identity_hash="deadbeef")
    lc.acquire_lock(loop_id, project_dir, "detached-worker", 300)

    notified: list[str] = []
    monkeypatch.setattr(
        lds, "notify_macos", lambda title, message: notified.append(message) or True
    )
    monkeypatch.setattr(scheduler, "lds", lds)

    stopped = scheduler.verify_repo_identity_at_startup(project_dir)

    assert stopped == []
    state = lc.load_state(loop_id, project_dir)
    assert state.status == "running"
    assert state.stop_reason is None
    assert notified == []
    journal_file = lc.journal_path(loop_id, project_dir)
    assert not journal_file.exists()


def test_verify_repo_identity_at_startup_stops_once_lease_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SN8: once the lease has actually expired (TTL elapsed, no live owner left), the
    deferred safety-stop from a previous startup call takes effect normally."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "deadbeef-issue-1"
    _seed_state(tmp_path, loop_id, status="running", repo_identity_hash="deadbeef")
    lc.acquire_lock(loop_id, project_dir, "detached-worker", 0)

    monkeypatch.setattr(lds, "notify_macos", lambda title, message: True)
    monkeypatch.setattr(scheduler, "lds", lds)

    stopped = scheduler.verify_repo_identity_at_startup(project_dir)

    assert stopped == [loop_id]
    state = lc.load_state(loop_id, project_dir)
    assert state.status == "stopped"
    assert state.stop_reason == "repo_identity_mismatch"


# --------------------------------------------------------------------------------------------
# respawn paths must recheck repo-identity before respawning (J1)
#
# `verify_repo_identity_at_startup` only runs once, at scheduler startup. SN8 deliberately
# leaves a mismatched loop whose lease was still alive at that moment neither stopped nor
# tracked, so it is only re-evaluated once its lease actually expires. Each of the three
# respawn paths below must perform that re-evaluation itself before spawning a worker for the
# loop_id, or it would spawn `loop_driver.py` for a loop belonging to a different repository.
# --------------------------------------------------------------------------------------------


def test_respawn_orphaned_active_loops_safety_stops_instead_of_respawning_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mismatched loop with no live lease (lease already expired, or absent entirely) must
    be safety-stopped here, not respawned, even though it is otherwise eligible."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "deadbeef-issue-1"
    _seed_state(tmp_path, loop_id, status="running", repo_identity_hash="deadbeef")
    runtime = scheduler.SchedulerRuntime()

    monkeypatch.setattr(lds, "notify_macos", lambda title, message: True)
    monkeypatch.setattr(scheduler, "lds", lds)

    def _fail_spawn(lid: str, project: str) -> None:
        raise AssertionError("must not respawn a repo-identity-mismatched loop")

    monkeypatch.setattr(scheduler, "spawn_worker", _fail_spawn)

    result = scheduler.respawn_orphaned_active_loops(runtime, project_dir)

    assert result == []
    assert loop_id not in runtime.workers
    assert loop_id in runtime.stopped_loop_ids
    state = lc.load_state(loop_id, project_dir)
    assert state.status == "stopped"
    assert state.stop_reason == "repo_identity_mismatch"


def test_respawn_expired_cooldowns_safety_stops_instead_of_respawning_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo-identity-mismatched loop must not be respawned just because its foreign-lease
    cooldown elapsed - exercised via `reap_finished_workers`, which delegates to
    `_respawn_expired_cooldowns` once no worker needs reaping."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "deadbeef-issue-1"
    _seed_state(tmp_path, loop_id, status="running", repo_identity_hash="deadbeef")
    runtime = scheduler.SchedulerRuntime(foreign_lease_cooldown_until={loop_id: 500.0})
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: 1000.0)  # cooldown already elapsed

    monkeypatch.setattr(lds, "notify_macos", lambda title, message: True)
    monkeypatch.setattr(scheduler, "lds", lds)

    def _fail_spawn(lid: str, project: str) -> None:
        raise AssertionError("must not respawn a repo-identity-mismatched loop")

    monkeypatch.setattr(scheduler, "spawn_worker", _fail_spawn)

    result = scheduler.reap_finished_workers(runtime, project_dir)

    assert result == []
    assert loop_id not in runtime.workers
    assert loop_id not in runtime.foreign_lease_cooldown_until
    state = lc.load_state(loop_id, project_dir)
    assert state.status == "stopped"
    assert state.stop_reason == "repo_identity_mismatch"


def test_reap_finished_workers_safety_stops_instead_of_crash_restarting_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An abnormal-exit crash-restart candidate (RH4's immediate-restart branch) must also be
    rechecked for repo-identity before being restarted."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "deadbeef-issue-1"
    _seed_state(tmp_path, loop_id, status="running", repo_identity_hash="deadbeef")
    runtime = scheduler.SchedulerRuntime(workers={loop_id: _FakePopen(returncode=1)})

    monkeypatch.setattr(lds, "notify_macos", lambda title, message: True)
    monkeypatch.setattr(scheduler, "lds", lds)

    def _fail_spawn(lid: str, project: str) -> None:
        raise AssertionError("must not respawn a repo-identity-mismatched loop")

    monkeypatch.setattr(scheduler, "spawn_worker", _fail_spawn)

    result = scheduler.reap_finished_workers(runtime, project_dir)

    assert result == []
    assert loop_id not in runtime.workers
    state = lc.load_state(loop_id, project_dir)
    assert state.status == "stopped"
    assert state.stop_reason == "repo_identity_mismatch"


def test_reap_finished_workers_defers_crash_restart_when_mismatch_lease_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the mismatch is detected but the loop's lease is still alive (e.g. another process
    re-acquired it concurrently), neither respawn nor safety-stop happens this cycle - deferred
    to a later cycle, mirroring SN8's own deferral in `verify_repo_identity_at_startup`."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "deadbeef-issue-1"
    _seed_state(tmp_path, loop_id, status="running", repo_identity_hash="deadbeef")
    lc.acquire_lock(loop_id, project_dir, "someone-else", 300)
    runtime = scheduler.SchedulerRuntime(workers={loop_id: _FakePopen(returncode=1)})

    def _fail_spawn(lid: str, project: str) -> None:
        raise AssertionError("must not respawn a repo-identity-mismatched loop")

    monkeypatch.setattr(scheduler, "spawn_worker", _fail_spawn)

    result = scheduler.reap_finished_workers(runtime, project_dir)

    assert result == []
    assert loop_id not in runtime.workers
    assert loop_id not in runtime.stopped_loop_ids
    state = lc.load_state(loop_id, project_dir)
    assert state.status == "running"
    assert state.stop_reason is None


# --------------------------------------------------------------------------------------------
# _safe_stop_repo_identity_mismatch: stale pre-coord-lock read must be re-validated (RH1)
# --------------------------------------------------------------------------------------------


def test_safe_stop_repo_identity_mismatch_skips_write_when_purged_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RH1: `verify_repo_identity_at_startup` necessarily reads `state` *before* acquiring the
    per-loop coord lock (the lock is keyed on `state.loop_id`, only known once state.json has
    already been read once). If a concurrent purge (tombstone-then-rmtree, RH3) completes
    entirely in the window between that read and this call actually acquiring the coord lock,
    the loop's lock.json is gone too - `_is_lease_expired` then returns True (no lock file), so
    the existing lease-liveness check alone does not catch this stale-purge race. Without
    re-validating after the coord lock is actually held, this call would write a brand-new
    `stopped` state.json that resurrects the already-deleted, now-tombstoned directory."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "deadbeef-issue-1"
    state = _seed_state(tmp_path, loop_id, status="running", repo_identity_hash="deadbeef")
    expected = wm.resolve_repo_identity_hash(project_dir)

    # Simulate a concurrent purge (state dir gone + tombstone written) having completed
    # entirely in the window between `state` being read above and the coord lock being
    # acquired inside `_safe_stop_repo_identity_mismatch`.
    shutil.rmtree(lc.loop_dir(loop_id, project_dir))
    tombstone_path = lc.loop_root(project_dir) / f"{loop_id}.tombstone.json"
    tombstone_path.write_text(
        json.dumps({"loop_id": loop_id, "status": "passed", "purged_at": lc.now_iso()}),
        encoding="utf-8",
    )

    def _fail_notify(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not notify - the stop was never actually written")

    monkeypatch.setattr(lds, "notify_macos", _fail_notify)
    monkeypatch.setattr(scheduler, "lds", lds)

    result = scheduler._safe_stop_repo_identity_mismatch(state, project_dir, expected)

    assert result is False
    assert not lc.loop_dir(loop_id, project_dir).exists()


def test_safe_stop_repo_identity_mismatch_skips_write_when_status_became_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RH1: if the loop reached a terminal status (e.g. via its own worker's normal exit) in
    the window between the stale pre-lock read and this call acquiring the coord lock, the
    stale `state` argument must not be written through - the freshly-reloaded state is now
    terminal and the safety-stop no longer applies."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "deadbeef-issue-1"
    state = _seed_state(tmp_path, loop_id, status="running", repo_identity_hash="deadbeef")
    expected = wm.resolve_repo_identity_hash(project_dir)

    current = lc.load_state(loop_id, project_dir)
    current.status = "passed"
    current.state_version += 1
    lc._write_state(current, project_dir)

    monkeypatch.setattr(lds, "notify_macos", lambda *a, **k: True)
    monkeypatch.setattr(scheduler, "lds", lds)

    result = scheduler._safe_stop_repo_identity_mismatch(state, project_dir, expected)

    assert result is False
    assert lc.load_state(loop_id, project_dir).status == "passed"


def test_safe_stop_repo_identity_mismatch_writes_when_state_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RH1 regression guard: the reload-and-revalidate guard must not block the ordinary,
    uncontended case - when nothing raced the stale read, the safety-stop still writes
    normally."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "deadbeef-issue-1"
    state = _seed_state(tmp_path, loop_id, status="running", repo_identity_hash="deadbeef")
    expected = wm.resolve_repo_identity_hash(project_dir)

    monkeypatch.setattr(lds, "notify_macos", lambda *a, **k: True)
    monkeypatch.setattr(scheduler, "lds", lds)

    result = scheduler._safe_stop_repo_identity_mismatch(state, project_dir, expected)

    assert result is True
    written = lc.load_state(loop_id, project_dir)
    assert written.status == "stopped"
    assert written.stop_reason == "repo_identity_mismatch"


# --------------------------------------------------------------------------------------------
# run_scheduler: startup safety-stop feeds into the exclusion set, finite max_cycles
# --------------------------------------------------------------------------------------------


def test_run_scheduler_excludes_startup_stopped_loop_and_respects_max_cycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    project_dir = str(tmp_path)
    loop_id = "deadbeef-issue-1"
    _seed_state(tmp_path, loop_id, status="running", repo_identity_hash="deadbeef")

    monkeypatch.setattr(scheduler.time, "sleep", lambda seconds: None)
    cycle_calls: list[int] = []
    monkeypatch.setattr(
        scheduler, "run_cycle", lambda runtime, project, definition: cycle_calls.append(1)
    )

    scheduler.run_scheduler(project_dir, max_cycles=3)

    assert len(cycle_calls) == 3
    assert lc.load_state(loop_id, project_dir).status == "stopped"


def test_run_scheduler_continues_after_a_cycle_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#21: one poll cycle raising must not take the resident scheduler process down; it
    should be logged and the loop should continue to the next cycle."""
    _init_repo(tmp_path)
    project_dir = str(tmp_path)

    monkeypatch.setattr(scheduler.time, "sleep", lambda seconds: None)
    cycle_calls: list[int] = []

    def _flaky_cycle(runtime: object, project: str, definition: object) -> None:
        cycle_calls.append(1)
        if len(cycle_calls) == 2:
            raise RuntimeError("boom")

    monkeypatch.setattr(scheduler, "run_cycle", _flaky_cycle)

    scheduler.run_scheduler(project_dir, max_cycles=3)

    assert len(cycle_calls) == 3
    assert "boom" in capsys.readouterr().err


def test_run_scheduler_raises_for_unknown_definition(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    with pytest.raises(ld.DefinitionValidationError):
        scheduler.run_scheduler(str(tmp_path), "not-a-real-definition", max_cycles=1)


# --------------------------------------------------------------------------------------------
# cron / launchd templates (3.5 節)
# --------------------------------------------------------------------------------------------


def test_render_launchd_plist_contains_keepalive_and_paths(tmp_path: Path) -> None:
    plist = scheduler.render_launchd_plist(str(tmp_path))
    assert "<key>KeepAlive</key>" in plist
    assert "<true/>" in plist
    assert str(tmp_path.resolve()) in plist
    assert "loop_scheduler.py" in plist
    assert "com.ai-orchestra.loop-scheduler" in plist


def test_render_launchd_plist_uses_sys_executable_not_hardcoded_python3(tmp_path: Path) -> None:
    """#G8: a launchd job started with `/usr/bin/python3` cannot import a venv/uv-managed
    dependency the scheduler was actually installed under, so a hardcoded system interpreter
    would make the resident job die immediately on start."""
    plist = scheduler.render_launchd_plist(str(tmp_path))
    assert scheduler.xml_escape(sys.executable) in plist
    assert "/usr/bin/python3" not in plist


def test_render_launchd_plist_honors_explicit_python_bin_override(tmp_path: Path) -> None:
    plist = scheduler.render_launchd_plist(str(tmp_path), python_bin="/custom/venv/bin/python3")
    assert "/custom/venv/bin/python3" in plist
    assert sys.executable not in plist


def test_render_cron_entry_contains_pgrep_guard_and_project_path(tmp_path: Path) -> None:
    entry = scheduler.render_cron_entry(str(tmp_path))
    assert "pgrep -f" in entry
    assert str(tmp_path.resolve()) in entry
    assert entry.strip().startswith("*/5 * * * *")


def test_render_cron_entry_uses_sys_executable_not_hardcoded_python3(tmp_path: Path) -> None:
    """#G8: mirrors the same fix in `render_launchd_plist` for the cron fallback command."""
    entry = scheduler.render_cron_entry(str(tmp_path))
    tokens = shlex.split(entry)
    assert sys.executable in tokens
    assert "/usr/bin/python3" not in entry


def test_render_cron_entry_honors_explicit_python_bin_override(tmp_path: Path) -> None:
    entry = scheduler.render_cron_entry(str(tmp_path), python_bin="/custom/venv/bin/python3")
    tokens = shlex.split(entry)
    assert "/custom/venv/bin/python3" in tokens


# --------------------------------------------------------------------------------------------
# --definition in cron/launchd templates (#H15)
# --------------------------------------------------------------------------------------------


def test_render_launchd_plist_omits_definition_flag_for_default(tmp_path: Path) -> None:
    plist = scheduler.render_launchd_plist(str(tmp_path))
    assert "--definition" not in plist


def test_render_launchd_plist_includes_definition_flag_for_non_default(tmp_path: Path) -> None:
    plist = scheduler.render_launchd_plist(str(tmp_path), definition_id="custom-loop")
    assert "--definition" in plist
    assert "custom-loop" in plist


def test_render_cron_entry_omits_definition_flag_for_default(tmp_path: Path) -> None:
    entry = scheduler.render_cron_entry(str(tmp_path))
    assert "--definition" not in entry


def test_render_cron_entry_includes_definition_flag_for_non_default(tmp_path: Path) -> None:
    entry = scheduler.render_cron_entry(str(tmp_path), definition_id="custom-loop")
    tokens = shlex.split(entry)
    assert "--definition" in tokens
    assert "custom-loop" in tokens


def test_main_print_launchd_and_print_cron_forward_definition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = scheduler.main(
        ["--project", str(tmp_path), "--definition", "custom-loop", "print-launchd"]
    )
    assert exit_code == 0
    assert "custom-loop" in capsys.readouterr().out

    exit_code = scheduler.main(
        ["--project", str(tmp_path), "--definition", "custom-loop", "print-cron"]
    )
    assert exit_code == 0
    assert "custom-loop" in capsys.readouterr().out


# --------------------------------------------------------------------------------------------
# multi-project pgrep guard / launchd label uniqueness (#H16)
# --------------------------------------------------------------------------------------------


def test_render_launchd_plist_label_is_unique_per_project(tmp_path: Path) -> None:
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()

    plist_a = scheduler.render_launchd_plist(str(project_a))
    plist_b = scheduler.render_launchd_plist(str(project_b))

    def _label(plist: str) -> str:
        start = plist.index("<key>Label</key>")
        return plist[start : start + 200].split("<string>")[1].split("</string>")[0]

    assert _label(plist_a) != _label(plist_b)
    assert _label(plist_a).startswith("com.ai-orchestra.loop-scheduler.")


def test_render_launchd_plist_label_stable_for_same_project(tmp_path: Path) -> None:
    plist_1 = scheduler.render_launchd_plist(str(tmp_path))
    plist_2 = scheduler.render_launchd_plist(str(tmp_path))
    assert plist_1 == plist_2


def test_render_cron_entry_pgrep_pattern_includes_project_path(tmp_path: Path) -> None:
    """#H16: the pgrep guard must match on `--project <path>` too, not just the script path,
    so a second project sharing the same `loop_scheduler.py` script cannot be mistaken for
    this project's already-running scheduler.

    The project path is embedded `re.escape`-d (SM2, since `pgrep -f` treats its pattern as a
    regex), so this checks for the escaped form rather than the raw path string."""
    entry = scheduler.render_cron_entry(str(tmp_path))
    pgrep_index = entry.index("pgrep -f ")
    pgrep_arg_end = entry.index(" || ", pgrep_index)
    pgrep_arg = entry[pgrep_index + len("pgrep -f ") : pgrep_arg_end]
    assert "--project" in pgrep_arg
    assert re.escape(str(tmp_path.resolve())) in pgrep_arg


def test_render_cron_entry_pgrep_pattern_escapes_regex_metacharacters(tmp_path: Path) -> None:
    """SM2: `pgrep -f <pattern>` treats <pattern> as a POSIX extended regular expression, not
    a literal string. An unescaped project path containing ERE metacharacters (all legal in a
    filesystem path) would either fail to match at all (e.g. an unbalanced `(`) or match
    something other than the literal path intended."""
    project = tmp_path / "issue (42)+x[y]?a|b.c^d$e"
    project.mkdir()

    entry = scheduler.render_cron_entry(str(project))

    pgrep_index = entry.index("pgrep -f ")
    pgrep_arg_end = entry.index(" || ", pgrep_index)
    pgrep_arg = entry[pgrep_index + len("pgrep -f ") : pgrep_arg_end]
    # The raw (unescaped) path must not appear verbatim in the pgrep pattern...
    assert str(project.resolve()) not in pgrep_arg
    # ...but its regex-escaped form must, so `pgrep -f` matches it as a literal string.
    assert re.escape(str(project.resolve())) in pgrep_arg
    # The whole rendered pgrep pattern must itself compile as a valid regex (an unescaped
    # unbalanced `(` in the raw path would otherwise raise `re.error` here).
    re.compile(pgrep_arg.strip("'\""))


def test_render_cron_entry_pgrep_pattern_differs_across_projects(tmp_path: Path) -> None:
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()

    entry_a = scheduler.render_cron_entry(str(project_a))
    entry_b = scheduler.render_cron_entry(str(project_b))

    def _pgrep_arg(entry: str) -> str:
        start = entry.index("pgrep -f ")
        end = entry.index(" || ", start)
        return entry[start:end]

    assert _pgrep_arg(entry_a) != _pgrep_arg(entry_b)


# --------------------------------------------------------------------------------------------
# definition id in cron pgrep guard / launchd label uniqueness (J4/J6)
# --------------------------------------------------------------------------------------------


def test_render_cron_entry_pgrep_pattern_includes_definition_id_for_non_default(
    tmp_path: Path,
) -> None:
    """J4: without the definition id in the pgrep pattern, an already-running scheduler for a
    *different* loop definition in the same project is mistaken for "this definition's
    scheduler is already alive", and the cron entry never starts the requested definition's
    own scheduler."""
    entry = scheduler.render_cron_entry(str(tmp_path), definition_id="custom-loop")
    pgrep_index = entry.index("pgrep -f ")
    pgrep_arg_end = entry.index(" || ", pgrep_index)
    pgrep_arg = entry[pgrep_index + len("pgrep -f ") : pgrep_arg_end]
    assert "--definition" in pgrep_arg
    # SM2: embedded `re.escape`-d, mirroring `script`/`project` above (a hyphen is escaped too).
    assert re.escape("custom-loop") in pgrep_arg


def test_render_cron_entry_pgrep_pattern_omits_definition_id_for_default(
    tmp_path: Path,
) -> None:
    entry = scheduler.render_cron_entry(str(tmp_path))
    pgrep_index = entry.index("pgrep -f ")
    pgrep_arg_end = entry.index(" || ", pgrep_index)
    pgrep_arg = entry[pgrep_index + len("pgrep -f ") : pgrep_arg_end]
    assert "--definition" not in pgrep_arg


def test_render_cron_entry_pgrep_pattern_differs_across_definitions(tmp_path: Path) -> None:
    """J4: two definitions in the same project must not collide on the same liveness guard,
    or a running scheduler for one definition would block the other's cron entry from ever
    starting its own scheduler."""
    entry_default = scheduler.render_cron_entry(str(tmp_path))
    entry_custom = scheduler.render_cron_entry(str(tmp_path), definition_id="custom-loop")

    def _pgrep_arg(entry: str) -> str:
        start = entry.index("pgrep -f ")
        end = entry.index(" || ", start)
        return entry[start:end]

    assert _pgrep_arg(entry_default) != _pgrep_arg(entry_custom)


# --------------------------------------------------------------------------------------------
# cron pgrep guard: self/ancestor-PID exclusion (Issue #219 P2-3, #13 follow-up)
# --------------------------------------------------------------------------------------------


def _extract_pgrep_guard_filter_suffix(entry: str) -> str:
    """Return the ` | grep -vxF -e "$$" -e "$PPID" | grep -q .` suffix `render_cron_entry`
    appends after the `pgrep -f '<pattern>'` clause, verbatim as rendered (not hand-copied), so
    the following shell-semantics tests exercise the *actual* rendered text."""
    filter_start = entry.index("| grep -vxF")
    guard_end = entry.index(" || ", filter_start)
    return entry[filter_start:guard_end]


def test_render_cron_entry_pgrep_guard_includes_self_and_parent_pid_exclusion_filter(
    tmp_path: Path,
) -> None:
    """Issue #219 P2-3: the rendered pgrep guard clause must pipe through a `$$`/`$PPID`
    exclusion filter before the `|| <fallback>`, so the wrapping `/bin/sh -c` cron process's
    own argv (which literally contains the fallback command's text, and therefore always
    self-matches the raw `pgrep -f <pattern>` alone) cannot make a dead scheduler look alive
    forever."""
    entry = scheduler.render_cron_entry(str(tmp_path))
    filter_suffix = _extract_pgrep_guard_filter_suffix(entry)
    assert filter_suffix == '| grep -vxF -e "$$" -e "$PPID" | grep -q .'


def test_pgrep_guard_filter_falls_back_when_only_the_wrapper_shells_own_pid_matched(
    tmp_path: Path,
) -> None:
    """Real shell-semantics regression for the #13 self-match bug: when `pgrep -f` finds only
    the wrapping shell's own PID (simulating the documented failure mode -- the cron wrapper's
    argv contains the fallback command's text, so it always self-matches the bare pattern),
    the exclusion filter must reduce that to "nothing found" and let the `||` fallback run.

    Piping a fixed, single-line `printf` in place of the real `pgrep -f <pattern>` call lets
    this test deterministically control what pgrep "found" without depending on this test
    process's own OS/pgrep visibility semantics (observed to vary across environments) or
    spawning a real scheduler subprocess."""
    entry = scheduler.render_cron_entry(str(tmp_path))
    filter_suffix = _extract_pgrep_guard_filter_suffix(entry)
    # `$PPID` inside the `sh -c` subshell below is this pipeline's own parent -- the outer
    # shell running this test command -- exactly mirroring how the real bug's only "match" is
    # the cron wrapper shell (this subshell's own parent), not a genuinely different process.
    command = f'printf "%s\\n" "$PPID" {filter_suffix} && echo REAL_MATCH || echo FALLBACK_RAN'
    completed = subprocess.run(
        ["sh", "-c", command], capture_output=True, text=True, timeout=10, check=False
    )
    assert completed.stdout.strip() == "FALLBACK_RAN"


def test_pgrep_guard_filter_skips_fallback_when_a_genuinely_different_pid_remains(
    tmp_path: Path,
) -> None:
    """The exclusion filter must not blanket-suppress every match -- a PID that is neither this
    shell's own `$$` nor its `$PPID` (a genuinely different, still-alive scheduler process)
    must still be recognized, so the guard does not spawn a duplicate scheduler."""
    entry = scheduler.render_cron_entry(str(tmp_path))
    filter_suffix = _extract_pgrep_guard_filter_suffix(entry)
    command = (
        f'printf "%s\\n%s\\n" "$PPID" 999999 {filter_suffix} && '
        "echo REAL_MATCH || echo FALLBACK_RAN"
    )
    completed = subprocess.run(
        ["sh", "-c", command], capture_output=True, text=True, timeout=10, check=False
    )
    assert completed.stdout.strip() == "REAL_MATCH"


def _launchd_label(rendered_plist: str) -> str:
    start = rendered_plist.index("<key>Label</key>")
    return rendered_plist[start : start + 200].split("<string>")[1].split("</string>")[0]


def test_render_launchd_plist_label_includes_definition_id_for_non_default(
    tmp_path: Path,
) -> None:
    """J6: without the definition id in the label, generating plists for both the default
    loop and a non-default `--definition` in the same project produces two plists with an
    identical `Label`. `launchd.plist(5)` requires `Label` to uniquely identify the job to
    launchd, so loading the second collides with the first."""
    plist = scheduler.render_launchd_plist(str(tmp_path), definition_id="custom-loop")
    assert _launchd_label(plist).endswith(".custom-loop")


def test_render_launchd_plist_label_omits_definition_id_for_default(tmp_path: Path) -> None:
    plist = scheduler.render_launchd_plist(str(tmp_path))
    assert "custom-loop" not in _launchd_label(plist)


def test_render_launchd_plist_label_differs_across_definitions(tmp_path: Path) -> None:
    plist_default = scheduler.render_launchd_plist(str(tmp_path))
    plist_custom = scheduler.render_launchd_plist(str(tmp_path), definition_id="custom-loop")
    assert _launchd_label(plist_default) != _launchd_label(plist_custom)


# --------------------------------------------------------------------------------------------
# log-dir creation before redirection (#H17)
# --------------------------------------------------------------------------------------------


def test_render_cron_entry_creates_log_dir_before_redirect(tmp_path: Path) -> None:
    """#H17: `.claude/loop/` may not exist yet on a fresh checkout; the shell's `>>` redirect
    at the end of the cron line fails outright if its parent directory is missing."""
    entry = scheduler.render_cron_entry(str(tmp_path))
    assert entry.strip().startswith("*/5 * * * * mkdir -p")
    mkdir_index = entry.index("mkdir -p ")
    and_index = entry.index(" && ", mkdir_index)
    mkdir_arg = entry[mkdir_index + len("mkdir -p ") : and_index]
    assert mkdir_arg == shlex.quote(f"{tmp_path.resolve()}/.claude/loop")


def test_render_launchd_plist_creates_log_dir_before_exec(tmp_path: Path) -> None:
    """#H17: launchd's `StandardOutPath`/`StandardErrorPath` redirection fails to start the
    job at all if its parent directory does not exist yet."""
    plist = scheduler.render_launchd_plist(str(tmp_path))
    command_start = plist.index("<string>mkdir -p")
    command_end = plist.index("</string>", command_start)
    command = plist[command_start + len("<string>") : command_end]
    assert command.startswith("mkdir -p")
    assert " &amp;&amp; exec " in command  # XML-escaped `&&`
    assert str(tmp_path.resolve()) in command


def test_render_launchd_plist_escapes_xml_special_characters(tmp_path: Path) -> None:
    """#12: an unescaped `&`/`<`/`>` in project_dir would otherwise produce invalid plist
    XML."""
    project = tmp_path / "a & b <weird>"
    project.mkdir()
    plist = scheduler.render_launchd_plist(str(project))
    assert "a & b <weird>" not in plist
    assert "a &amp; b &lt;weird&gt;" in plist
    import xml.etree.ElementTree as ET

    ET.fromstring(plist)  # must parse as well-formed XML


def test_render_cron_entry_shell_quotes_project_path_with_special_characters(
    tmp_path: Path,
) -> None:
    """#12: an unquoted project_dir containing shell metacharacters would otherwise split
    into extra tokens or inject additional commands into the crontab line."""
    project = tmp_path / "weird; rm -rf ~ #"
    project.mkdir()
    entry = scheduler.render_cron_entry(str(project))
    # If the path were interpolated unquoted, whitespace/`;`/`#` would split it into several
    # separate shlex tokens instead of one; a properly quoted path stays a single token.
    tokens = shlex.split(entry)
    assert str(project.resolve()) in tokens


def test_render_cron_entry_escapes_percent_in_project_path(tmp_path: Path) -> None:
    """SN7: `crontab` treats an unescaped `%` as a newline at crontab-file-parsing time (before
    the shell ever sees the line), splitting the command and feeding the remainder to stdin. A
    project path containing a literal `%` (a legal filesystem-path character) must not be able
    to truncate/split the rendered cron entry."""
    project = tmp_path / "100%-done"
    project.mkdir()

    entry = scheduler.render_cron_entry(str(project))

    assert "\\%" in entry
    assert entry.count("\n") == 1  # only the trailing newline; no unescaped `%` split it
    # Every literal `%` in the resolved path must be escaped, not left bare.
    unescaped = entry.replace("\\%", "")
    assert "%" not in unescaped


@pytest.mark.parametrize("bad_char", ["\n", "\r"])
def test_render_cron_entry_fails_closed_on_cr_lf_in_project_path(
    tmp_path: Path, bad_char: str
) -> None:
    """SN-cron: unlike `%` (escaped to `\\%`, SN7), an embedded CR/LF cannot be escaped away at
    the crontab-file-parsing stage - `shlex.quote` only protects *shell* parsing of the
    already-assembled line, not crontab's own line-splitting of the raw file. A project_dir
    containing a literal CR/LF (legal in a POSIX filename, if vanishingly rare) must fail
    closed instead of silently rendering a malformed multi-line crontab entry."""
    project_dir = f"{tmp_path}{bad_char}evil"

    with pytest.raises(ValueError, match="CR/LF"):
        scheduler.render_cron_entry(project_dir)


@pytest.mark.parametrize("bad_char", ["\r", "\x0b", "\x1f"])
def test_render_launchd_plist_fails_closed_on_cr_or_control_chars_in_project_path(
    tmp_path: Path, bad_char: str
) -> None:
    """RM3: unlike cron's CR/LF fail-closed guard (SN-cron), a lone LF is XML-1.0-legal and
    passes through XML parsing completely unchanged, so it is *not* rejected here (see the
    sibling `..._allows_lone_lf_...` test below). CR and other C0 control characters outside
    the small XML-1.0-legal set (TAB/LF/CR) must still fail closed: a literal CR is not itself
    illegal XML content, but XML 1.0's mandatory line-ending normalization (CR/CRLF/lone-CR
    folded to LF) would silently change the interpolated value the moment any XML parser
    (including launchd's own plist reader) reads this file back; any other rejected control
    character is outright illegal XML 1.0 content, producing an unparseable plist."""
    project_dir = f"{tmp_path}{bad_char}evil"

    with pytest.raises(ValueError, match="control characters"):
        scheduler.render_launchd_plist(project_dir)


def test_render_launchd_plist_allows_lone_lf_in_project_path(tmp_path: Path) -> None:
    """RM3: a lone LF is XML-1.0-legal and passes through XML parsing completely unchanged (no
    normalization applies to it, unlike CR) - it must not be rejected the way CR/other control
    characters are."""
    project = tmp_path / "weird\nname"
    project.mkdir(parents=True)

    plist = scheduler.render_launchd_plist(str(project))

    import xml.etree.ElementTree as ET

    ET.fromstring(plist)  # must still parse as well-formed XML


def test_main_print_launchd_and_print_cron(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = scheduler.main(["--project", str(tmp_path), "print-launchd"])
    assert exit_code == 0
    assert "KeepAlive" in capsys.readouterr().out

    exit_code = scheduler.main(["--project", str(tmp_path), "print-cron"])
    assert exit_code == 0
    assert "pgrep -f" in capsys.readouterr().out


def test_main_print_launchd_creates_log_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SN4: launchd opens `StandardOutPath`/`StandardErrorPath` before exec'ing
    `ProgramArguments`, so the in-template `mkdir -p ... && exec ...` wrapper (#H17) likely
    runs too late; the log dir must already exist by the time `print-launchd` is run."""
    log_dir = tmp_path / ".claude" / "loop"
    assert not log_dir.exists()

    exit_code = scheduler.main(["--project", str(tmp_path), "print-launchd"])

    assert exit_code == 0
    capsys.readouterr()
    assert log_dir.is_dir()


def test_main_print_cron_creates_log_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SN4: mirrors the `print-launchd` fix for `print-cron`."""
    log_dir = tmp_path / ".claude" / "loop"
    assert not log_dir.exists()

    exit_code = scheduler.main(["--project", str(tmp_path), "print-cron"])

    assert exit_code == 0
    capsys.readouterr()
    assert log_dir.is_dir()


# --------------------------------------------------------------------------------------------
# spawn_worker: exact child argv (3.3 節)
# --------------------------------------------------------------------------------------------


def test_spawn_worker_builds_expected_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_popen(cmd: list[str], **kwargs: object) -> _FakePopen:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakePopen(None)

    monkeypatch.setattr(scheduler.subprocess, "Popen", fake_popen)

    scheduler.spawn_worker("abcd1234-issue-1", "/some/project")

    cmd = captured["cmd"]
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("loop_driver.py")
    assert cmd[2:] == [
        "--loop-id",
        "abcd1234-issue-1",
        "--project",
        "/some/project",
        "--definition",
        scheduler.DEFAULT_DEFINITION_ID,
    ]
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL
    assert captured["kwargs"]["start_new_session"] is True


def test_spawn_worker_forwards_definition_id_to_child_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#G3: `--definition` must reflect the definition actually used for discovery, not always
    the hardcoded `DEFAULT_DEFINITION_ID` - otherwise a scheduler started with a custom
    `--definition` silently falls back to `issue-loop` for every brand-new loop."""
    captured: dict[str, object] = {}

    def fake_popen(cmd: list[str], **kwargs: object) -> _FakePopen:
        captured["cmd"] = cmd
        return _FakePopen(None)

    monkeypatch.setattr(scheduler.subprocess, "Popen", fake_popen)

    scheduler.spawn_worker("abcd1234-issue-1", "/some/project", "custom-loop")

    cmd = captured["cmd"]
    assert cmd[2:] == [
        "--loop-id",
        "abcd1234-issue-1",
        "--project",
        "/some/project",
        "--definition",
        "custom-loop",
    ]
