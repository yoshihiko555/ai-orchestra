"""Lock, fencing, attach, and file placement tests for loop_common."""

from __future__ import annotations

import json
import os
import subprocess
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
    result = lc.propose("abcd1234-issue-1", project_dir, "wrong-token")
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
    assert state.pending_action is None
    assert any(
        event["event"] == "stopped" and event["payload"]["stop_reason"] == "foreign_live_lease"
        for event in events
    )


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
