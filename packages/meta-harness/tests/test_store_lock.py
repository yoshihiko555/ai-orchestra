"""`store.lock` のテスト（EV-28 部分, Sec2-3）。lock/stale 判定は決定論的（実 sleep 禁止）。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_lock",
    "packages/meta-harness/lib/meta_harness_common.py",
)


def _lock_path(main_root: Path, config: dict) -> Path:
    return mh.locks_dir(main_root, config) / "store.lock"


class TestStoreLockAcquireRelease:
    def test_lock_file_exists_with_pid_while_held(self, tmp_path: Path) -> None:
        config = {"locks": {"store_ttl_seconds": 60}}
        with mh.store_lock(tmp_path, config):
            lock_path = _lock_path(tmp_path, config)
            assert lock_path.is_file()
            assert lock_path.read_text(encoding="utf-8").startswith(f"{os.getpid()}:")

    def test_lock_file_removed_after_context_exits(self, tmp_path: Path) -> None:
        config = {"locks": {"store_ttl_seconds": 60}}
        with mh.store_lock(tmp_path, config):
            pass
        assert not _lock_path(tmp_path, config).is_file()

    def test_lock_file_removed_even_if_body_raises(self, tmp_path: Path) -> None:
        config = {"locks": {"store_ttl_seconds": 60}}
        try:
            with mh.store_lock(tmp_path, config):
                raise ValueError("boom")
        except ValueError:
            pass
        assert not _lock_path(tmp_path, config).is_file()


class TestStoreLockContention:
    def test_double_acquire_while_held_raises(self, tmp_path: Path) -> None:
        config = {"locks": {"store_ttl_seconds": 60}}
        lock_path = _lock_path(tmp_path, config)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            str(os.getpid()), encoding="utf-8"
        )  # fresh mtime, held by "another process"

        try:
            with mh.store_lock(tmp_path, config):
                pass
        except mh.LockAcquisitionError:
            pass
        else:
            raise AssertionError("acquiring an already-held fresh lock should raise")


class TestStoreLockStaleness:
    def test_stale_lock_older_than_ttl_is_stolen(self, tmp_path: Path) -> None:
        config = {"locks": {"store_ttl_seconds": 60}}
        lock_path = _lock_path(tmp_path, config)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("99999", encoding="utf-8")
        stale_time = time.time() - 120  # TTL(60s) より古い
        os.utime(lock_path, (stale_time, stale_time))

        with mh.store_lock(tmp_path, config):
            assert lock_path.read_text(encoding="utf-8").startswith(f"{os.getpid()}:")

    def test_lock_newer_than_ttl_is_not_stolen(self, tmp_path: Path) -> None:
        config = {"locks": {"store_ttl_seconds": 60}}
        lock_path = _lock_path(tmp_path, config)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("99999", encoding="utf-8")
        fresh_time = time.time() - 5  # TTL(60s) より新しい
        os.utime(lock_path, (fresh_time, fresh_time))

        try:
            with mh.store_lock(tmp_path, config):
                pass
        except mh.LockAcquisitionError:
            pass
        else:
            raise AssertionError("a lock younger than the TTL should not be stolen")

    def test_is_lock_stale_boundary(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "store.lock"
        lock_path.write_text("1", encoding="utf-8")
        now = time.time()
        os.utime(lock_path, (now - 61, now - 61))
        assert mh._is_lock_stale(lock_path, ttl_seconds=60) is True

        os.utime(lock_path, (now - 1, now - 1))
        assert mh._is_lock_stale(lock_path, ttl_seconds=60) is False

    def test_is_lock_stale_missing_file_is_stale(self, tmp_path: Path) -> None:
        assert mh._is_lock_stale(tmp_path / "does-not-exist.lock", ttl_seconds=60) is True


class TestStoreLockCompareAndDeleteOnRelease:
    def test_release_does_not_delete_lock_when_content_no_longer_matches_own_token(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        config = {"locks": {"store_ttl_seconds": 60}}
        lock_path = _lock_path(tmp_path, config)

        def acquire_with_fixed_token(lock_file: Path, ttl_seconds: float) -> str:
            lock_file.write_text("token-a", encoding="utf-8")
            return "token-a"

        monkeypatch.setattr(mh, "_acquire_store_lock", acquire_with_fixed_token)

        with mh.store_lock(tmp_path, config):
            assert lock_path.read_text(encoding="utf-8") == "token-a"
            lock_path.write_text("token-b", encoding="utf-8")

        assert lock_path.is_file()
        assert lock_path.read_text(encoding="utf-8") == "token-b"

    def test_release_deletes_lock_when_content_still_matches_own_token(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        config = {"locks": {"store_ttl_seconds": 60}}
        lock_path = _lock_path(tmp_path, config)

        def acquire_with_fixed_token(lock_file: Path, ttl_seconds: float) -> str:
            lock_file.write_text("token-a", encoding="utf-8")
            return "token-a"

        monkeypatch.setattr(mh, "_acquire_store_lock", acquire_with_fixed_token)

        with mh.store_lock(tmp_path, config):
            assert lock_path.read_text(encoding="utf-8") == "token-a"

        assert not lock_path.is_file()


class TestStoreLockTakeoverCompareBeforeUnlink:
    # PR #162 レビュー指摘 (FIX E): stale lock 奪取は compare-before-unlink であるべき
    # （stale 判定時に読んだ token 内容 + mtime を、unlink 直前に再読して一致する場合のみ
    # unlink する）。real sleep は使わず、内容の書き換えだけで決定論的に検証する。
    def test_takeover_skips_unlink_when_lock_content_changed_before_takeover(
        self, tmp_path: Path
    ) -> None:
        lock_path = tmp_path / "store.lock"
        lock_path.write_text("token-a", encoding="utf-8")
        snapshot = mh._read_lock_snapshot(lock_path)

        # 別プロセスが奪取直前に unlink + 再作成した状況を模す
        lock_path.write_text("token-b", encoding="utf-8")

        mh._unlink_if_unchanged(lock_path, snapshot)

        assert lock_path.is_file()
        assert lock_path.read_text(encoding="utf-8") == "token-b"

    def test_takeover_unlinks_when_lock_content_unchanged(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "store.lock"
        lock_path.write_text("token-a", encoding="utf-8")
        snapshot = mh._read_lock_snapshot(lock_path)

        mh._unlink_if_unchanged(lock_path, snapshot)

        assert not lock_path.is_file()

    def test_read_lock_snapshot_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert mh._read_lock_snapshot(tmp_path / "does-not-exist.lock") is None


class TestStoreLockSnapshotFirstOrdering:
    # PR #162 レビュー指摘 (FIX P1): staleness 判定は snapshot-first でなければならない。
    # 旧実装（別々のタイミングで stat → 内容再読）だと、判定と snapshot 取得の間に
    # 別プロセスが lock を fresh に差し替えると、fresh lock が誤って奪取されてしまう。
    def test_snapshot_first_prevents_stealing_lock_that_became_fresh_after_race(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        lock_path = tmp_path / "store.lock"
        lock_path.write_text("stale-token", encoding="utf-8")
        stale_time = time.time() - 120
        os.utime(lock_path, (stale_time, stale_time))

        original_read_snapshot = mh._read_lock_snapshot
        call_count = {"n": 0}

        def racy_read_snapshot(path: Path):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # 別プロセスが、まさにこちらが snapshot を取る瞬間に fresh lock へ
                # 差し替えたことを模す。
                fresh_time = time.time()
                path.write_text("fresh-token", encoding="utf-8")
                os.utime(path, (fresh_time, fresh_time))
            return original_read_snapshot(path)

        monkeypatch.setattr(mh, "_read_lock_snapshot", racy_read_snapshot)

        try:
            mh._acquire_store_lock(lock_path, ttl_seconds=60)
        except mh.LockAcquisitionError:
            pass
        else:
            raise AssertionError("fresh lock (post-race) must not be stolen")

        assert lock_path.read_text(encoding="utf-8") == "fresh-token"

    def test_lock_disappearing_between_eexist_and_snapshot_read_retries_and_succeeds(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        lock_path = tmp_path / "store.lock"
        lock_path.write_text("held-by-someone-else", encoding="utf-8")

        original_read_snapshot = mh._read_lock_snapshot
        call_count = {"n": 0}

        def vanishing_read_snapshot(path: Path):
            call_count["n"] += 1
            if call_count["n"] == 1:
                path.unlink()  # lock が snapshot 取得の瞬間に消滅したことを模す
                return None
            return original_read_snapshot(path)

        monkeypatch.setattr(mh, "_read_lock_snapshot", vanishing_read_snapshot)

        token = mh._acquire_store_lock(lock_path, ttl_seconds=60)
        assert lock_path.read_text(encoding="utf-8") == token


class TestStoreLockCliExit3:
    def test_register_exits_3_when_lock_pre_held(
        self, git_project: Path, run_meta, default_overlay, tmp_path: Path
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        lock_path = _lock_path(git_project, config)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(str(os.getpid()), encoding="utf-8")

        overlay_dir = default_overlay(tmp_path)
        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            project=git_project,
            check=False,
        )

        assert result.returncode == 3

    def test_purge_exits_3_when_lock_pre_held(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        lock_path = _lock_path(git_project, config)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(str(os.getpid()), encoding="utf-8")

        result = run_meta("purge", project=git_project, check=False)

        assert result.returncode == 3

    def test_frontier_rebuild_exits_3_when_lock_pre_held(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        lock_path = _lock_path(git_project, config)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(str(os.getpid()), encoding="utf-8")

        result = run_meta("frontier", "--rebuild", project=git_project, check=False)

        assert result.returncode == 3
