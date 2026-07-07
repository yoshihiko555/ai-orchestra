"""`evaluate.lock` のテスト（EV-15, Sec2-3）。PID + heartbeat 方式。実 sleep は使わず、
mtime 注入で staleness 判定を決定論的に検証する。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_evaluate_lock",
    "packages/meta-harness/lib/meta_harness_common.py",
)


def _lock_path(main_root: Path, config: dict) -> Path:
    return mh.locks_dir(main_root, config) / "evaluate.lock"


_CONFIG = {
    "locks": {
        "evaluate_heartbeat_seconds": 999999,  # テスト中に heartbeat が発火しないよう十分大きく
        "evaluate_stale_seconds": 300,
    }
}


class TestEvaluateLockAcquireRelease:
    def test_lock_file_exists_with_token_while_held(self, tmp_path: Path) -> None:
        with mh.evaluate_lock(tmp_path, _CONFIG):
            lock_path = _lock_path(tmp_path, _CONFIG)
            assert lock_path.is_file()
            assert lock_path.read_text(encoding="utf-8").startswith(f"{os.getpid()}:")

    def test_lock_file_removed_after_context_exits(self, tmp_path: Path) -> None:
        with mh.evaluate_lock(tmp_path, _CONFIG):
            pass
        assert not _lock_path(tmp_path, _CONFIG).is_file()

    def test_lock_file_removed_even_if_body_raises(self, tmp_path: Path) -> None:
        try:
            with mh.evaluate_lock(tmp_path, _CONFIG):
                raise ValueError("boom")
        except ValueError:
            pass
        assert not _lock_path(tmp_path, _CONFIG).is_file()


class TestEvaluateLockContention:
    def test_double_acquire_while_held_raises(self, tmp_path: Path) -> None:
        lock_path = _lock_path(tmp_path, _CONFIG)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(str(os.getpid()), encoding="utf-8")  # fresh mtime

        try:
            with mh.evaluate_lock(tmp_path, _CONFIG):
                pass
        except mh.LockAcquisitionError:
            pass
        else:
            raise AssertionError("acquiring an already-held fresh evaluate.lock should raise")


class TestEvaluateLockStaleness:
    def test_stale_lock_older_than_threshold_is_stolen(self, tmp_path: Path) -> None:
        lock_path = _lock_path(tmp_path, _CONFIG)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("99999", encoding="utf-8")
        stale_time = time.time() - 600  # stale threshold(300s) より古い
        os.utime(lock_path, (stale_time, stale_time))

        with mh.evaluate_lock(tmp_path, _CONFIG):
            assert lock_path.read_text(encoding="utf-8").startswith(f"{os.getpid()}:")

    def test_lock_newer_than_threshold_is_not_stolen(self, tmp_path: Path) -> None:
        lock_path = _lock_path(tmp_path, _CONFIG)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("99999", encoding="utf-8")
        fresh_time = time.time() - 5  # stale threshold(300s) より新しい
        os.utime(lock_path, (fresh_time, fresh_time))

        try:
            with mh.evaluate_lock(tmp_path, _CONFIG):
                pass
        except mh.LockAcquisitionError:
            pass
        else:
            raise AssertionError("a lock younger than the stale threshold should not be stolen")


class TestEvaluateLockHeartbeat:
    def test_touch_evaluate_lock_updates_mtime(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "evaluate.lock"
        lock_path.write_text("token", encoding="utf-8")
        old_time = time.time() - 1000
        os.utime(lock_path, (old_time, old_time))

        mh._touch_evaluate_lock(lock_path)

        assert lock_path.stat().st_mtime > old_time

    def test_touch_evaluate_lock_missing_file_does_not_raise(self, tmp_path: Path) -> None:
        mh._touch_evaluate_lock(tmp_path / "does-not-exist.lock")  # must not raise

    def test_heartbeat_loop_stops_when_stop_event_set(self, tmp_path: Path) -> None:
        import threading

        lock_path = tmp_path / "evaluate.lock"
        lock_path.write_text("token", encoding="utf-8")
        stop_event = threading.Event()
        stop_event.set()  # 即座に停止させ、ループが1周もせず終了することを確認する

        mh._evaluate_lock_heartbeat_loop(lock_path, heartbeat_seconds=999999, stop_event=stop_event)
        # 例外なく即座に戻ってくること（wait() が最初のチェックで True を返し抜ける）。


class TestEvaluateLockCliExit3:
    def test_evaluate_exits_3_when_lock_pre_held(
        self, git_project: Path, run_meta, default_overlay, tmp_path: Path
    ) -> None:
        # ロック取得はケーパビリティゲートより前に行われるため、実 claude/codex には到達しない。
        run_meta("init", project=git_project, check=True)
        overlay_dir = default_overlay(tmp_path)
        import json as _json

        register_result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            "--json",
            project=git_project,
            check=True,
        )
        cand_id = _json.loads(register_result.stdout)["cand_id"]

        config = mh.load_config(git_project)
        lock_path = _lock_path(git_project, config)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(str(os.getpid()), encoding="utf-8")

        result = run_meta("evaluate", "--candidate", cand_id, project=git_project, check=False)

        assert result.returncode == 3
