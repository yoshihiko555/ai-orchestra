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
import shlex
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

    def fake_gh_list(repo: str, label: str) -> str:
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
        lambda repo, label: json.dumps(
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
    monkeypatch.setattr(scheduler, "_gh_list_issues", lambda repo, label: page_1 + page_2)

    issues = scheduler.list_labeled_issues("/some/project", "loop:issue")

    assert [item["number"] for item in issues] == [1, 2]


def test_list_labeled_issues_returns_empty_on_gh_failure_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed `gh` call must not raise and take the resident scheduler down with it (#34)."""
    monkeypatch.setattr(scheduler, "_repo_name_with_owner", lambda project_dir: "acme/widgets")
    monkeypatch.setattr(scheduler, "_gh_list_issues", lambda repo, label: "")

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
        return _FakeCompleted()

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)

    assert scheduler._gh_list_issues("acme/widgets", "loop:issue") == ""


def test_gh_list_issues_returns_empty_string_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `gh` call that exceeds the 30s timeout must not raise `TimeoutExpired` and take the
    resident scheduler down with it; it must honor the same "failure returns \"\"" contract as
    a nonzero exit (#F20)."""

    def fake_run(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="gh api ...", timeout=30)

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)

    assert scheduler._gh_list_issues("acme/widgets", "loop:issue") == ""


def test_gh_list_issues_returns_empty_string_on_missing_gh_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#H2: `gh` not being installed raises `FileNotFoundError` (an `OSError` subclass), not
    `TimeoutExpired`; without also catching `OSError` this would raise on every poll cycle
    instead of honoring the "failure returns \"\"" contract."""

    def fake_run(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("gh: command not found")

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)

    assert scheduler._gh_list_issues("acme/widgets", "loop:issue") == ""


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


def test_spawn_new_workers_spawns_only_up_to_available_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    this project's already-running scheduler."""
    entry = scheduler.render_cron_entry(str(tmp_path))
    pgrep_index = entry.index("pgrep -f ")
    pgrep_arg_end = entry.index(" || ", pgrep_index)
    pgrep_arg = entry[pgrep_index + len("pgrep -f ") : pgrep_arg_end]
    assert "--project" in pgrep_arg
    assert str(tmp_path.resolve()) in pgrep_arg


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


def test_main_print_launchd_and_print_cron(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = scheduler.main(["--project", str(tmp_path), "print-launchd"])
    assert exit_code == 0
    assert "KeepAlive" in capsys.readouterr().out

    exit_code = scheduler.main(["--project", str(tmp_path), "print-cron"])
    assert exit_code == 0
    assert "pgrep -f" in capsys.readouterr().out


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
