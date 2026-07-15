"""Lock, fencing, attach, and file placement tests for loop_common."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from tests.module_loader import load_module

lc = load_module("loop_common_lock", "packages/loop-harness/lib/loop_common.py")


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _write_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str = "running") -> str:
    monkeypatch.setattr(lc, "resolve_root_worktree", lambda _project_dir: tmp_path)
    monkeypatch.setattr(lc.socket, "gethostname", lambda: "local")
    monkeypatch.setattr(lc, "_repo_identity_hash", lambda _project_dir: "abcd1234")
    project_dir = str(tmp_path)
    state = lc._initial_state(
        "abcd1234-issue-1", "issue-loop", "abcd1234", "/tmp/wt", "loop/issue-1", "implementation"
    )
    state.status = status
    lc._write_state(state, project_dir)
    return project_dir


def test_validate_lease_requires_explicit_caller_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _write_state(tmp_path, monkeypatch)
    lock = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 3600, host="local")
    assert lock is not None
    assert lc.validate_lease("abcd1234-issue-1", project_dir, lock.lease_token) is True
    assert lc.validate_lease("abcd1234-issue-1", project_dir, "wrong-token") is False
    with pytest.raises(lc.WriteRejectedError):
        lc.propose("abcd1234-issue-1", project_dir, "wrong-token")


def test_acquire_lock_live_same_host_returns_none_and_foreign_host_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _write_state(tmp_path, monkeypatch)
    first = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 3600, host="host-a")
    assert first is not None
    assert lc.acquire_lock("abcd1234-issue-1", project_dir, "owner-2", 3600, host="host-a") is None
    with pytest.raises(lc.ForeignLeaseError):
        lc.acquire_lock("abcd1234-issue-1", project_dir, "owner-3", 3600, host="host-b")


def test_propose_returns_stop_for_live_foreign_host_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _write_state(tmp_path, monkeypatch)
    lock = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 3600, host="host-b")
    assert lock is not None
    monkeypatch.setattr(lc.socket, "gethostname", lambda: "host-a")
    with pytest.raises(lc.WriteRejectedError):
        lc.propose("abcd1234-issue-1", project_dir, "wrong-token")
    result = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    events = [
        json.loads(line)
        for line in lc.journal_path("abcd1234-issue-1", project_dir)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert result.action == lc.Action.STOP.value
    assert result.context["stop_reason"] == "foreign_live_lease"
    assert state.status == "stopped"
    assert state.stop_reason == "foreign_live_lease"
    assert state.pending_action is not None
    assert state.pending_action.action == lc.Action.STOP.value
    assert state.pending_action.action_id == result.action_id
    assert any(
        event["event"] == "stopped" and event["payload"]["stop_reason"] == "foreign_live_lease"
        for event in events
    )


def test_ensure_unchanged_since_rejects_write_after_concurrent_state_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DH1: `_ensure_unchanged_since` must reject a fenced write when state.json has moved
    on since the caller last read it, closing the gap between `propose`/`complete`/
    `reconcile`'s initial `load_state` and their eventual guarded write."""
    project_dir = _write_state(tmp_path, monkeypatch)
    lock = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 3600, host="local")
    assert lock is not None
    state = lc.load_state("abcd1234-issue-1", project_dir)

    lc._ensure_unchanged_since("abcd1234-issue-1", project_dir, state.state_version)

    state.state_version += 1
    lc._write_state(state, project_dir)
    with pytest.raises(lc.WriteRejectedError, match="state changed since read"):
        lc._ensure_unchanged_since("abcd1234-issue-1", project_dir, state.state_version - 1)


def test_propose_write_blocks_on_concurrent_flock_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DH1: `propose()`'s final write must hold the lock-file's own flock (via
    `guarded_lease_section`), so a concurrent flock holder on the same path is serialized
    against instead of raced. Before the fix, `_ensure_valid_lease` validated the lease and
    returned long before this write, leaving it unguarded."""
    project_dir = _write_state(tmp_path, monkeypatch, status="running")
    loop_id = "abcd1234-issue-1"
    lock = lc.acquire_lock(loop_id, project_dir, "owner", 3600, host="local")
    assert lock is not None
    lock_file = lc.lock_path(loop_id, project_dir)

    held = lock_file.open("r+", encoding="utf-8")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)

    events: list[str] = []

    def _propose() -> None:
        lc.propose(loop_id, project_dir, lock.lease_token)
        events.append("proposed")

    thread = threading.Thread(target=_propose)
    thread.start()
    try:
        time.sleep(0.2)
        assert events == []
        events.append("released")
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert events == ["released", "proposed"]
    state = lc.load_state(loop_id, project_dir)
    assert state.pending_action is not None


def test_is_lease_alive_uses_ttl_not_pid() -> None:
    lock = lc.LockInfo("owner", 2_000_000_000, "host", lc.now_iso(), lc.now_iso(), 3600, "token")
    assert lc.is_lease_alive(lock) is True
    stale = lc.LockInfo(
        "owner", os.getpid(), "host", lc.now_iso(), "1970-01-01T00:00:00+00:00", 1, "token"
    )
    assert lc.is_lease_alive(stale) is False


def test_acquire_lock_reclaims_stale_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = _write_state(tmp_path, monkeypatch)
    first = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 1, host="local")
    assert first is not None
    path = lc.lock_path("abcd1234-issue-1", project_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["heartbeat_at"] = "1970-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    second = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner-2", 3600, host="local")
    assert second is not None
    assert second.lease_token != first.lease_token


def test_resume_requires_failed_or_stopped_and_reset_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _write_state(tmp_path, monkeypatch, status="failed")
    lock = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 3600, host="local")
    assert lock is not None
    with pytest.raises(lc.InvalidStateError, match="reset_counters"):
        lc.resume("abcd1234-issue-1", project_dir, False, "owner-2", 3600)
    result = lc.resume("abcd1234-issue-1", project_dir, True, "owner-2", 3600)
    assert result.state.status == "running"
    assert result.lease_token != lock.lease_token
    with pytest.raises(lc.InvalidStateError, match="cannot resume"):
        lc.resume("abcd1234-issue-1", project_dir, True, "owner-3", 3600)


def test_resume_succeeds_when_lock_file_is_entirely_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SH2(b): `release_lock` deletes lock.json outright, so a `failed` loop's typical state
    has no lock file at all when `resume()` is called. `_replace_lock`'s `O_CREAT | O_EXCL`
    create branch (mirroring `acquire_lock`'s own discipline) must handle this cleanly."""
    project_dir = _write_state(tmp_path, monkeypatch, status="failed")
    lock_file = lc.lock_path("abcd1234-issue-1", project_dir)
    assert not lock_file.exists()

    result = lc.resume("abcd1234-issue-1", project_dir, True, "owner-2", 3600)

    assert result.state.status == "running"
    assert lock_file.is_file()
    fresh = lc._read_lock(lock_file)
    assert fresh is not None
    assert fresh.lease_token == result.lease_token


def test_replace_lock_blocks_on_concurrent_flock_holder_when_file_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SH2(b): `_replace_lock` (used by `resume`/`reacquire_lease`) must take this lock file's
    own flock before overwriting it - whether or not the file already exists - mirroring the
    `acquire_lock`/`_acquire_existing_lock` discipline. Without this, a concurrent flock holder
    on the same path (`loop_status.py` purge, which holds this same flock across its
    stale-status reload + directory delete precisely to guard against this race) could race an
    in-flight `resume()`/`reacquire_lease()` write instead of being serialized against it."""
    project_dir = _write_state(tmp_path, monkeypatch, status="failed")
    loop_id = "abcd1234-issue-1"
    lock = lc.acquire_lock(loop_id, project_dir, "owner", 3600, host="local")
    assert lock is not None
    lock_file = lc.lock_path(loop_id, project_dir)

    held = lock_file.open("r+", encoding="utf-8")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)

    events: list[str] = []

    def _resume() -> None:
        lc.resume(loop_id, project_dir, True, "owner-2", 3600)
        events.append("resumed")

    thread = threading.Thread(target=_resume)
    thread.start()
    try:
        time.sleep(0.2)
        # While this test still holds the flock, resume()'s `_replace_lock` write must be
        # blocked, not racing ahead to overwrite lock.json underneath it.
        assert events == []
        events.append("released")
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert events == ["released", "resumed"]
    assert lc.load_state(loop_id, project_dir).status == "running"


def test_resume_blocks_while_coord_lock_is_held_by_a_concurrent_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SN-flock: `resume`'s reload-through-write section is held under the same
    purge-independent `held_coord_lock` `loop_status._purge_if_still_safe` takes (see its
    docstring). Holding that fixed-path lock here (simulating a concurrent purge in-flight)
    must block `resume()`, not let it race ahead - unlike the inner lock.json flock alone,
    which stops protecting anything the instant a purge's `rmtree` swaps out that file's
    inode."""
    project_dir = _write_state(tmp_path, monkeypatch, status="failed")
    loop_id = "abcd1234-issue-1"
    lock = lc.acquire_lock(loop_id, project_dir, "owner", 3600, host="local")
    assert lock is not None
    coord_lock_file = lc.coord_lock_path(loop_id, project_dir)
    coord_lock_file.parent.mkdir(parents=True, exist_ok=True)
    held_fd = os.open(coord_lock_file, os.O_CREAT | os.O_RDWR, lc.FILE_MODE)
    fcntl.flock(held_fd, fcntl.LOCK_EX)

    events: list[str] = []

    def _resume() -> None:
        lc.resume(loop_id, project_dir, True, "owner-2", 3600)
        events.append("resumed")

    thread = threading.Thread(target=_resume)
    thread.start()
    try:
        time.sleep(0.2)
        # While this test still holds the coord lock, resume() must remain blocked.
        assert events == []
        events.append("released")
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert events == ["released", "resumed"]
    assert lc.load_state(loop_id, project_dir).status == "running"


def test_attach_blocks_while_coord_lock_is_held_by_a_concurrent_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SN-flock: `attach` (via `reacquire_lease`) must also block against the same
    purge-independent coord lock, mirroring `resume`'s equivalent test above."""
    project_dir = _write_state(tmp_path, monkeypatch, status="running")
    loop_id = "abcd1234-issue-1"
    lock = lc.acquire_lock(loop_id, project_dir, "owner", 1, host="local")
    assert lock is not None
    path = lc.lock_path(loop_id, project_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["heartbeat_at"] = "1970-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")

    coord_lock_file = lc.coord_lock_path(loop_id, project_dir)
    coord_lock_file.parent.mkdir(parents=True, exist_ok=True)
    held_fd = os.open(coord_lock_file, os.O_CREAT | os.O_RDWR, lc.FILE_MODE)
    fcntl.flock(held_fd, fcntl.LOCK_EX)

    events: list[str] = []

    def _attach() -> None:
        lc.attach(loop_id, project_dir, "owner-2", 3600)
        events.append("attached")

    thread = threading.Thread(target=_attach)
    thread.start()
    try:
        time.sleep(0.2)
        assert events == []
        events.append("released")
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert events == ["released", "attached"]


def test_attach_requires_existing_stale_lock_and_running_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _write_state(tmp_path, monkeypatch, status="running")
    lock = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 1, host="local")
    assert lock is not None
    with pytest.raises(lc.ForeignLeaseError):
        lc.attach("abcd1234-issue-1", project_dir, "owner-2", 3600)

    path = lc.lock_path("abcd1234-issue-1", project_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["heartbeat_at"] = "1970-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    result = lc.attach("abcd1234-issue-1", project_dir, "owner-2", 3600)
    assert result.context["lease_token"] != lock.lease_token
    assert result.action == lc.Action.RUN_MAKER.value


def test_attach_recovers_orphaned_maker_pending_after_stale_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _write_state(tmp_path, monkeypatch, status="running")
    lock = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 1, host="local")
    assert lock is not None
    pending = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    assert pending.action == lc.Action.RUN_MAKER.value

    path = lc.lock_path("abcd1234-issue-1", project_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["heartbeat_at"] = "1970-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")

    result = lc.attach("abcd1234-issue-1", project_dir, "owner-2", 3600)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    assert state.last_check_result["infrastructure_failure"] is True
    assert result.action == lc.Action.RUN_MAKER.value


def test_attach_recovers_pending_status_with_orphaned_initial_maker_after_stale_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #205: a session that crashes between `start`'s initial `run_maker` proposal and
    its `complete` call leaves `state.status == "pending"` with an orphaned pending action and
    no recovery entry point (`resume` is `failed`/`stopped`-only). `attach` must now recover
    this the same way it already recovers an orphaned `run_maker` pending action while
    `status == "running"` (see `test_attach_recovers_orphaned_maker_pending_after_stale_lease`
    above): mark it an infrastructure failure via reconcile, then re-propose `run_maker`."""
    project_dir = _write_state(tmp_path, monkeypatch, status="pending")
    lock = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 1, host="local")
    assert lock is not None
    pending = lc.propose("abcd1234-issue-1", project_dir, lock.lease_token)
    assert pending.action == lc.Action.RUN_MAKER.value

    path = lc.lock_path("abcd1234-issue-1", project_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["heartbeat_at"] = "1970-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")

    result = lc.attach("abcd1234-issue-1", project_dir, "owner-2", 3600)
    state = lc.load_state("abcd1234-issue-1", project_dir)
    assert state.status == "pending"
    assert state.last_check_result["infrastructure_failure"] is True
    assert result.action == lc.Action.RUN_MAKER.value
    assert result.context["lease_token"] != lock.lease_token


def test_attach_rejects_pending_with_live_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #205 safety: pending recovery must not bypass the existing live-lease guard - a
    still-alive owner (TTL within, heartbeat continuing) must still block attach with
    `ForeignLeaseError` (exit 3 at the CLI layer) exactly as it does for `running`."""
    project_dir = _write_state(tmp_path, monkeypatch, status="pending")
    lock = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 1, host="local")
    assert lock is not None
    with pytest.raises(lc.ForeignLeaseError):
        lc.attach("abcd1234-issue-1", project_dir, "owner-2", 3600)


def test_attach_missing_lock_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = _write_state(tmp_path, monkeypatch, status="running")
    with pytest.raises(lc.LockNotFoundError):
        lc.attach("abcd1234-issue-1", project_dir, "owner", 3600)


def test_state_journal_lock_are_owner_only_and_under_root_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _write_state(tmp_path, monkeypatch)
    lock = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 3600, host="local")
    assert lock is not None
    lc.append_journal_event(
        "abcd1234-issue-1", project_dir, "pending", "step", "act-1", {"token": "Bearer abc"}
    )
    for path in (
        lc.state_path("abcd1234-issue-1", project_dir),
        lc.lock_path("abcd1234-issue-1", project_dir),
        lc.journal_path("abcd1234-issue-1", project_dir),
    ):
        assert str(path).startswith(str(tmp_path / ".claude" / "loop"))
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert "[REDACTED]" in lc.journal_path("abcd1234-issue-1", project_dir).read_text(
        encoding="utf-8"
    )


def test_start_rejects_existing_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = _write_state(tmp_path, monkeypatch)
    with pytest.raises(lc.InvalidStateError, match="state already exists"):
        lc.start(
            "abcd1234-issue-1",
            project_dir,
            "issue-loop",
            "abcd1234",
            "/tmp/wt",
            "loop/issue-1",
            "owner",
            3600,
        )


def test_loop_root_fails_closed_when_git_root_cannot_be_resolved(tmp_path: Path) -> None:
    lc._ROOT_CACHE.clear()
    with pytest.raises(lc.RootResolutionError):
        lc.loop_root(str(tmp_path))


def test_artifact_path_rejects_unsafe_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lc, "resolve_root_worktree", lambda _project_dir: tmp_path)
    with pytest.raises(ValueError, match="Unsafe loop_id"):
        lc.artifact_path("../loop", str(tmp_path), "act-1", "x.log")
    with pytest.raises(ValueError, match="Unsafe action_id"):
        lc.artifact_path("abcd1234-issue-1", str(tmp_path), "../act", "x.log")


def test_state_journal_lock_paths_reject_unsafe_loop_id(tmp_path: Path) -> None:
    for path_func in (lc.state_path, lc.journal_path, lc.lock_path):
        with pytest.raises(ValueError, match="Unsafe loop_id"):
            path_func("../loop", str(tmp_path))


def test_new_lock_creation_uses_single_os_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _write_state(tmp_path, monkeypatch)
    calls: list[bytes] = []
    real_write = os.write

    def spy_write(fd: int, data: bytes) -> int:
        calls.append(data)
        return real_write(fd, data)

    monkeypatch.setattr(lc.os, "write", spy_write)
    lock = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 3600, host="local")

    assert lock is not None
    assert len(calls) == 1
    payload = json.loads(calls[0].decode("utf-8"))
    assert payload["lease_token"] == lock.lease_token
    assert lc._read_lock(lc.lock_path("abcd1234-issue-1", project_dir)) == lock


def test_loop_root_resolves_main_worktree_from_real_git_worktree(tmp_path: Path) -> None:
    main = tmp_path / "repo"
    main.mkdir()
    _git(["init", "-b", "main"], main)
    _git(["config", "user.email", "loop-harness@example.com"], main)
    _git(["config", "user.name", "Loop Harness Test"], main)
    (main / "README.md").write_text("root\n", encoding="utf-8")
    _git(["add", "README.md"], main)
    _git(["commit", "-m", "init"], main)
    linked = tmp_path / "linked"
    _git(["worktree", "add", "-b", "loop/issue-1", str(linked), "HEAD"], main)

    lc._ROOT_CACHE.clear()

    assert lc.resolve_root_worktree(str(linked)) == main.resolve()
    assert lc.loop_root(str(linked)) == main.resolve() / ".claude" / "loop"


def test_heartbeat_updates_lock_without_touching_state_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _write_state(tmp_path, monkeypatch)
    lock = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 3600, host="local")
    assert lock is not None
    before = lc.load_state("abcd1234-issue-1", project_dir).state_version
    time.sleep(0.01)
    assert lc.heartbeat_lock("abcd1234-issue-1", project_dir, lock.lease_token) is True
    after = lc.load_state("abcd1234-issue-1", project_dir).state_version
    assert after == before


def test_concurrent_reacquire_lease_serializes_and_rejects_loser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DC2: staleness-check + write must happen inside one flock section so two concurrent
    `attach` callers cannot both end up believing they own the reacquired lease. Before the
    fix, the staleness check (`_read_lock`) ran outside the flock and the post-write re-read
    was a *second*, separately-flocked `_read_lock` call; a losing thread's re-read could
    observe the winner's freshly-written token and return it as if it were its own lease.
    With the fix, exactly one concurrent caller wins (gets a fresh `LockInfo` matching what
    is durably on disk) and the other observes the winner's now-live lease and raises
    `ForeignLeaseError` -- never both "succeeding" with conflicting beliefs of ownership."""
    project_dir = _write_state(tmp_path, monkeypatch, status="running")
    loop_id = "abcd1234-issue-1"
    old = lc.acquire_lock(loop_id, project_dir, "owner", 1, host="local")
    assert old is not None
    path = lc.lock_path(loop_id, project_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["heartbeat_at"] = "1970-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def _reacquire(owner: str) -> None:
        barrier.wait()
        try:
            results[owner] = lc.reacquire_lease(loop_id, project_dir, owner, 3600, host="local")
        except lc.ForeignLeaseError as exc:
            results[owner] = exc

    t1 = threading.Thread(target=_reacquire, args=("owner-a",))
    t2 = threading.Thread(target=_reacquire, args=("owner-b",))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive()
    assert not t2.is_alive()
    winners = [v for v in results.values() if isinstance(v, lc.LockInfo)]
    losers = [v for v in results.values() if isinstance(v, lc.ForeignLeaseError)]
    assert len(winners) == 1
    assert len(losers) == 1
    on_disk = lc._read_lock(path)
    assert on_disk == winners[0]


def test_old_lease_cannot_release_or_heartbeat_after_reacquire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _write_state(tmp_path, monkeypatch, status="running")
    old = lc.acquire_lock("abcd1234-issue-1", project_dir, "owner", 1, host="local")
    assert old is not None
    path = lc.lock_path("abcd1234-issue-1", project_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["heartbeat_at"] = "1970-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    new = lc.reacquire_lease("abcd1234-issue-1", project_dir, "owner-2", 3600)

    assert lc.heartbeat_lock("abcd1234-issue-1", project_dir, old.lease_token) is False
    assert lc.release_lock("abcd1234-issue-1", project_dir, old.lease_token) is False
    assert lc.validate_lease("abcd1234-issue-1", project_dir, new.lease_token) is True
