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
            assert lock_path.read_text(encoding="utf-8") == str(os.getpid())

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
            assert lock_path.read_text(encoding="utf-8") == str(os.getpid())

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
