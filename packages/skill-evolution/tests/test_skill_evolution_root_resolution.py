"""skill-evolution の root worktree 解決と legacy metrics migration のテスト。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.module_loader import load_module

se = load_module(
    "skill_evolution_common_root_resolution",
    "packages/skill-evolution/lib/skill_evolution_common.py",
)


def _make_project(path: Path) -> Path:
    """`.claude` を持つプロジェクトルートを用意する。"""
    (path / ".claude").mkdir(parents=True)
    return path


def _patch_root_worktree(monkeypatch, root_dir: Path | None) -> None:
    """skill-evolution が動的参照する共通 root 解決結果を差し替える。"""
    resolved = str(root_dir) if root_dir is not None else None
    hook_common = se._load_hook_common()
    se._resolve_log_root_cached.cache_clear()
    monkeypatch.setattr(
        hook_common,
        "resolve_root_worktree",
        lambda _project_dir, **_kwargs: resolved,
    )


# EV-37: worktree の metrics を root worktree の新配置へ集約する。
def test_worktree_metrics_are_written_to_root_log(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)

    se.append_metric(str(worktree), "issue-fix", {"run_id": "root-1", "success": True})

    expected = root / ".claude" / "logs" / "skill-evolution" / "metrics" / "issue-fix.jsonl"
    assert se.metrics_path(str(worktree), "issue-fix") == str(expected)
    assert [record["run_id"] for record in se.read_metrics(str(worktree), "issue-fix")] == [
        "root-1"
    ]
    assert expected.is_file()
    assert not (worktree / ".claude" / "logs" / "skill-evolution").exists()


# EV-37: root 解決不能時は project_dir の新配置へ fail-safe で戻る。
def test_root_resolution_failure_writes_metrics_to_project(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path / "project")
    _patch_root_worktree(monkeypatch, None)

    se.append_metric(str(project), "review", {"run_id": "fallback-1", "success": True})

    expected = project / ".claude" / "logs" / "skill-evolution" / "metrics" / "review.jsonl"
    assert se.metrics_path(str(project), "review") == str(expected)
    assert expected.is_file()


# EV-37: lessons は root 解決の対象外で、従来の worktree-local 配置を維持する。
def test_lessons_remain_worktree_local(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)

    se.append_lesson(str(worktree), "issue-fix", "worktree lesson")

    expected = worktree / ".claude" / "skill-evolution" / "lessons" / "issue-fix.md"
    assert se.lessons_path(str(worktree), "issue-fix") == str(expected)
    assert "worktree lesson" in se.read_lessons(str(worktree), "issue-fix")
    assert expected.is_file()
    assert not (root / ".claude" / "skill-evolution" / "lessons").exists()


# PR331 レビュー対応: pending と locks は project_dir ローカル解決（root 集約しない）。
# 複数 worktree 間で共有すると、他 worktree の実行中セッションを stale と誤判定して
# 回収してしまうため（metrics のみ root 集約する）。
def test_pending_and_locks_resolve_to_project_local(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)
    run_id = "issue-fix-20260101T000000-aaaa"

    se.write_pending(str(worktree), run_id, skill="issue-fix")
    assert se.acquire_lock(str(worktree), "issue-fix") is True

    expected_pending = (
        worktree / ".claude" / "logs" / "skill-evolution" / "pending" / f"{run_id}.json"
    )
    expected_lock = worktree / ".claude" / "logs" / "skill-evolution" / "locks" / "issue-fix.lock"
    assert se.pending_path(str(worktree), run_id) == str(expected_pending)
    assert se.lock_path(str(worktree), "issue-fix") == str(expected_lock)
    assert expected_pending.is_file()
    assert expected_lock.is_file()
    assert se.list_pending(str(worktree))[0]["run_id"] == run_id
    # root 側には作成されないこと
    assert not (root / ".claude" / "logs" / "skill-evolution" / "pending").exists()
    assert not (root / ".claude" / "logs" / "skill-evolution" / "locks").exists()


# EV-37: legacy metrics は一度だけ移行し、stale claim はそのまま残す。
def test_legacy_metrics_are_migrated_once_and_stale_claim_is_untouched(
    monkeypatch, tmp_path
) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)
    legacy_dir = Path(se.data_dir(str(worktree))) / "metrics"
    legacy_dir.mkdir(parents=True)
    legacy_path = legacy_dir / "issue-fix.jsonl"
    legacy_bytes = (
        json.dumps({"run_id": "legacy-1", "success": True}).encode()
        + b"\n"
        + json.dumps({"run_id": "legacy-2", "success": False}).encode()
        + b"\n"
    )
    legacy_path.write_bytes(legacy_bytes)
    stale_claim = legacy_dir / "review.jsonl.migrating.old-process"
    stale_bytes = b'{"run_id":"stale"}\n'
    stale_claim.write_bytes(stale_bytes)

    first = se.read_metrics(str(worktree), "issue-fix")

    destination = root / ".claude" / "logs" / "skill-evolution" / "metrics" / "issue-fix.jsonl"
    migrated = list(legacy_dir.glob("issue-fix.jsonl.migrated.*"))
    assert [record["run_id"] for record in first] == ["legacy-1", "legacy-2"]
    assert destination.is_file()
    assert not legacy_path.exists()
    assert len(migrated) == 1
    assert migrated[0].read_bytes() == legacy_bytes
    assert stale_claim.read_bytes() == stale_bytes

    second = se.read_metrics(str(worktree), "issue-fix")

    assert [record["run_id"] for record in second] == ["legacy-1", "legacy-2"]
    assert destination.read_bytes() == legacy_bytes
    assert stale_claim.read_bytes() == stale_bytes


# PR331 round 3: hook_common がなくても project-local の legacy metrics は移行する。
def test_legacy_metrics_migrate_without_hook_common(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path / "project")
    legacy_dir = Path(se.data_dir(str(project))) / "metrics"
    legacy_dir.mkdir(parents=True)
    legacy_path = legacy_dir / "issue-fix.jsonl"
    legacy_bytes = json.dumps({"run_id": "legacy-local", "success": True}).encode() + b"\n"
    legacy_path.write_bytes(legacy_bytes)
    se._resolve_log_root_cached.cache_clear()
    monkeypatch.setattr(se, "_load_hook_common", lambda: None)

    records = se.read_metrics(str(project), "issue-fix")

    destination = project / ".claude" / "logs" / "skill-evolution" / "metrics" / "issue-fix.jsonl"
    assert [record["run_id"] for record in records] == ["legacy-local"]
    assert destination.read_bytes() == legacy_bytes
    assert not legacy_path.exists()
    assert len(list(legacy_dir.glob("issue-fix.jsonl.migrated.*"))) == 1


# PR331 round 3: linked worktree から root checkout 自身の legacy metrics も移行する。
def test_root_legacy_metrics_are_migrated_from_linked_worktree(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)
    root_legacy_dir = Path(se.data_dir(str(root))) / "metrics"
    root_legacy_dir.mkdir(parents=True)
    root_legacy_path = root_legacy_dir / "issue-fix.jsonl"
    legacy_bytes = json.dumps({"run_id": "legacy-root", "success": True}).encode() + b"\n"
    root_legacy_path.write_bytes(legacy_bytes)

    records = se.read_metrics(str(worktree), "issue-fix")

    destination = root / ".claude" / "logs" / "skill-evolution" / "metrics" / "issue-fix.jsonl"
    assert [record["run_id"] for record in records] == ["legacy-root"]
    assert destination.read_bytes() == legacy_bytes
    assert not root_legacy_path.exists()
    assert len(list(root_legacy_dir.glob("issue-fix.jsonl.migrated.*"))) == 1
    assert not (Path(se.data_dir(str(worktree))) / "metrics").exists()


# PR331 round 4: root 解決失敗中に worktree の新配置へ残った metrics も root へ集約する。
def test_project_local_new_layout_metrics_are_reaggregated(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)
    stranded_dir = Path(se._local_logs_dir(str(worktree))) / "metrics"
    stranded_dir.mkdir(parents=True)
    stranded_path = stranded_dir / "issue-fix.jsonl"
    stranded_bytes = json.dumps({"run_id": "stranded-1", "success": True}).encode() + b"\n"
    stranded_path.write_bytes(stranded_bytes)

    records = se.read_metrics(str(worktree), "issue-fix")

    destination = root / ".claude" / "logs" / "skill-evolution" / "metrics" / "issue-fix.jsonl"
    assert [record["run_id"] for record in records] == ["stranded-1"]
    assert destination.read_bytes() == stranded_bytes
    assert not stranded_path.exists()
    assert len(list(stranded_dir.glob("issue-fix.jsonl.migrated.*"))) == 1


# PR331 round 4: 短い write は残りを再試行せず、claim を手動復旧用に残す。
def test_metric_migration_aborts_on_short_write(monkeypatch, tmp_path) -> None:
    legacy_path = tmp_path / "legacy.jsonl"
    destination_path = tmp_path / "destination" / "legacy.jsonl"
    payload = b'{"run_id":"first"}\n{"run_id":"second"}\n'
    legacy_path.write_bytes(payload)
    original_write = os.write
    write_calls: list[bytes] = []

    def _short_write(fd: int, remaining: bytes) -> int:
        write_calls.append(remaining)
        if len(write_calls) == 1:
            short_length = len(remaining) // 2
            return original_write(fd, remaining[:short_length])
        return original_write(fd, remaining)

    monkeypatch.setattr(se.os, "write", _short_write)

    se._migrate_metric_file(str(legacy_path), str(destination_path))

    migrating = list(tmp_path.glob("legacy.jsonl.migrating.*"))
    assert write_calls == [payload, b"\n"]
    assert destination_path.read_bytes() == payload[: len(payload) // 2] + b"\n"
    assert len(migrating) == 1
    assert migrating[0].read_bytes() == payload
    assert list(tmp_path.glob("legacy.jsonl.migrated.*")) == []


# PR331 round 3: 旧ファイルの未終端行は destination で改行終端する。
def test_metric_migration_adds_missing_trailing_newline(tmp_path) -> None:
    legacy_path = tmp_path / "legacy.jsonl"
    destination_path = tmp_path / "destination" / "legacy.jsonl"
    payload = b'{"run_id":"legacy"}'
    legacy_path.write_bytes(payload)

    se._migrate_metric_file(str(legacy_path), str(destination_path))

    assert destination_path.read_bytes() == payload + b"\n"
    assert len(list(tmp_path.glob("legacy.jsonl.migrated.*"))) == 1
    assert list(tmp_path.glob("legacy.jsonl.migrating.*")) == []


# PR331 round 4: payload write の例外時も改行だけ best-effort で追記して claim を残す。
def test_metric_migration_keeps_claim_when_payload_write_raises(monkeypatch, tmp_path) -> None:
    legacy_path = tmp_path / "legacy.jsonl"
    destination_path = tmp_path / "destination" / "legacy.jsonl"
    payload = b'{"run_id":"legacy"}\n'
    legacy_path.write_bytes(payload)
    original_write = os.write
    write_calls: list[bytes] = []

    def _raise_then_repair(fd: int, remaining: bytes) -> int:
        write_calls.append(remaining)
        if len(write_calls) == 1:
            raise OSError("simulated migration write failure")
        return original_write(fd, remaining)

    monkeypatch.setattr(se.os, "write", _raise_then_repair)

    se._migrate_metric_file(str(legacy_path), str(destination_path))

    migrating = list(tmp_path.glob("legacy.jsonl.migrating.*"))
    assert write_calls == [payload, b"\n"]
    assert len(migrating) == 1
    assert migrating[0].read_bytes() == payload
    assert list(tmp_path.glob("legacy.jsonl.migrated.*")) == []
    assert destination_path.read_bytes() == b"\n"


# PR331 round 2: write が進捗しなければ claim を手動復旧用に残す。
def test_metric_migration_keeps_claim_when_write_makes_no_progress(monkeypatch, tmp_path) -> None:
    legacy_path = tmp_path / "legacy.jsonl"
    destination_path = tmp_path / "destination" / "legacy.jsonl"
    payload = b'{"run_id":"legacy"}\n'
    legacy_path.write_bytes(payload)
    monkeypatch.setattr(se.os, "write", lambda _fd, _payload: 0)

    se._migrate_metric_file(str(legacy_path), str(destination_path))

    migrating = list(tmp_path.glob("legacy.jsonl.migrating.*"))
    assert len(migrating) == 1
    assert migrating[0].read_bytes() == payload
    assert list(tmp_path.glob("legacy.jsonl.migrated.*")) == []


# PR331 round 4: os.fchmod がない環境でも migration は完了する。
def test_metric_migration_succeeds_without_fchmod(monkeypatch, tmp_path) -> None:
    legacy_path = tmp_path / "legacy.jsonl"
    destination_path = tmp_path / "destination" / "legacy.jsonl"
    payload = b'{"run_id":"legacy"}\n'
    legacy_path.write_bytes(payload)
    monkeypatch.delattr(se.os, "fchmod", raising=False)

    se._migrate_metric_file(str(legacy_path), str(destination_path))

    assert destination_path.read_bytes() == payload
    assert len(list(tmp_path.glob("legacy.jsonl.migrated.*"))) == 1
    assert list(tmp_path.glob("legacy.jsonl.migrating.*")) == []


# EV-37: 移行先に既存レコードがあっても legacy は merge 追記される（意図的な挙動）。
def test_migration_merges_into_existing_destination(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)

    destination = root / ".claude" / "logs" / "skill-evolution" / "metrics" / "issue-fix.jsonl"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(json.dumps({"run_id": "existing-1", "success": True}).encode() + b"\n")

    legacy_dir = Path(se.data_dir(str(worktree))) / "metrics"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "issue-fix.jsonl").write_bytes(
        json.dumps({"run_id": "legacy-1", "success": True}).encode() + b"\n"
    )

    records = se.read_metrics(str(worktree), "issue-fix")

    assert sorted(record["run_id"] for record in records) == ["existing-1", "legacy-1"]


# PR3 回帰: 非 worktree（resolve_root_worktree が project_dir 自身を返す）環境でも
# legacy metrics ディレクトリ（.claude/skill-evolution/metrics）は destination
# ディレクトリ（.claude/logs/skill-evolution/metrics）と実体パスが異なるため移行される。
# 旧実装は log_root == project_root の一致だけで早期 return しており、非 worktree
# 環境（大多数）で旧 metrics が永久に移行されない回帰があった。
def test_legacy_metrics_migrate_in_non_worktree_repo(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path / "project")
    _patch_root_worktree(monkeypatch, project)
    legacy_dir = Path(se.data_dir(str(project))) / "metrics"
    legacy_dir.mkdir(parents=True)
    legacy_path = legacy_dir / "issue-fix.jsonl"
    legacy_bytes = (
        json.dumps({"run_id": "legacy-1", "success": True}).encode()
        + b"\n"
        + json.dumps({"run_id": "legacy-2", "success": False}).encode()
        + b"\n"
    )
    legacy_path.write_bytes(legacy_bytes)

    records = se.read_metrics(str(project), "issue-fix")

    destination = project / ".claude" / "logs" / "skill-evolution" / "metrics" / "issue-fix.jsonl"
    migrated = list(legacy_dir.glob("issue-fix.jsonl.migrated.*"))
    assert [record["run_id"] for record in records] == ["legacy-1", "legacy-2"]
    assert destination.is_file()
    assert not legacy_path.exists()
    assert len(migrated) == 1
    assert migrated[0].read_bytes() == legacy_bytes


# EV-37: migration は pending/locks/lessons を移動せず、metrics だけを扱う。
def test_migration_leaves_transient_state_and_lessons_at_legacy_path(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)
    legacy_root = Path(se.data_dir(str(worktree)))
    legacy_metrics = legacy_root / "metrics" / "review.jsonl"
    legacy_pending = legacy_root / "pending" / "run-1.json"
    legacy_lock = legacy_root / "locks" / "review.lock"
    legacy_metrics.parent.mkdir(parents=True)
    legacy_pending.parent.mkdir(parents=True)
    legacy_lock.parent.mkdir(parents=True)
    legacy_metrics.write_text('{"run_id":"legacy"}\n', encoding="utf-8")
    legacy_pending.write_text('{"run_id":"run-1"}\n', encoding="utf-8")
    legacy_lock.write_text("locked\n", encoding="utf-8")
    se.append_lesson(str(worktree), "review", "legacy lesson")

    assert [record["run_id"] for record in se.read_metrics(str(worktree), "review")] == ["legacy"]

    new_root = root / ".claude" / "logs" / "skill-evolution"
    assert legacy_pending.read_text(encoding="utf-8") == '{"run_id":"run-1"}\n'
    assert legacy_lock.read_text(encoding="utf-8") == "locked\n"
    assert not (new_root / "pending" / "run-1.json").exists()
    assert not (new_root / "locks" / "review.lock").exists()
    assert "legacy lesson" in se.read_lessons(str(worktree), "review")
    assert (legacy_root / "lessons" / "review.md").is_file()


# EV-37: 1 MiB を超える legacy metrics は部分行を捨て、完全な末尾行だけ移行する。
def test_metrics_migration_caps_tail_at_line_boundary(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)
    legacy_path = Path(se.data_dir(str(worktree))) / "metrics" / "large.jsonl"
    legacy_path.parent.mkdir(parents=True)
    tail_record = b'{"run_id":"tail","success":true}\n'
    original = b"x" * se.MIGRATION_MAX_BYTES + b"\n" + tail_record
    legacy_path.write_bytes(original)

    records = se.read_metrics(str(worktree), "large")

    assert [record["run_id"] for record in records] == ["tail"]
    migrated = list(legacy_path.parent.glob("large.jsonl.migrated.*"))
    assert len(migrated) == 1
    assert migrated[0].read_bytes() == original


# EV-37 / Critical: legacy metrics の symlink は移行対象にせず、参照先も変更しない。
def test_legacy_metrics_symlink_is_left_untouched(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)
    legacy_dir = Path(se.data_dir(str(worktree))) / "metrics"
    legacy_dir.mkdir(parents=True)

    real_path = legacy_dir / "real.jsonl"
    real_payload = b'{"run_id":"real"}\n'
    real_path.write_bytes(real_payload)
    sentinel_path = worktree / "sentinel.jsonl"
    sentinel_payload = b'{"run_id":"sentinel"}\n'
    sentinel_path.write_bytes(sentinel_payload)
    symlink_path = legacy_dir / "linked.jsonl"
    os.symlink(sentinel_path, symlink_path)

    real_records = se.read_metrics(str(worktree), "real")
    linked_records = se.read_metrics(str(worktree), "linked")

    destination = root / ".claude" / "logs" / "skill-evolution" / "metrics" / "real.jsonl"
    assert [record["run_id"] for record in real_records] == ["real"]
    assert destination.read_bytes() == real_payload
    assert len(list(legacy_dir.glob("real.jsonl.migrated.*"))) == 1
    assert symlink_path.is_symlink()
    assert symlink_path.resolve() == sentinel_path.resolve()
    assert sentinel_path.is_file()
    assert sentinel_path.read_bytes() == sentinel_payload
    assert linked_records == []
    assert list(legacy_dir.glob("linked.jsonl.migrating.*")) == []
    assert list(legacy_dir.glob("linked.jsonl.migrated.*")) == []


# EV-37 / Performance: 1 プロセスの移行予算を超える legacy は次回へ繰り越す。
def test_legacy_metrics_migration_defers_files_over_total_budget(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)
    legacy_dir = Path(se.data_dir(str(worktree))) / "metrics"
    legacy_dir.mkdir(parents=True)
    payload = b'{"run_id":"legacy"}\n'
    for name in ("a.jsonl", "b.jsonl", "c.jsonl"):
        (legacy_dir / name).write_bytes(payload)
    monkeypatch.setattr(se, "MIGRATION_TOTAL_BUDGET_BYTES", len(payload) + 1)

    se.migrate_legacy_metrics(str(worktree))

    destination_dir = root / ".claude" / "logs" / "skill-evolution" / "metrics"
    assert (destination_dir / "a.jsonl").read_bytes() == payload
    assert len(list(legacy_dir.glob("a.jsonl.migrated.*"))) == 1
    for name in ("b.jsonl", "c.jsonl"):
        untouched = legacy_dir / name
        assert untouched.read_bytes() == payload
        assert list(legacy_dir.glob(f"{name}.migrating.*")) == []
        assert list(legacy_dir.glob(f"{name}.migrated.*")) == []


# EV-37 / Performance: 空ファイルでも 1 回の migration 対象数を上限内に抑える。
def test_legacy_metrics_migration_caps_files_per_run(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)
    legacy_dir = Path(se.data_dir(str(worktree))) / "metrics"
    legacy_dir.mkdir(parents=True)
    names = [f"{index}.jsonl" for index in range(5)]
    for name in names:
        (legacy_dir / name).write_bytes(b"")
    monkeypatch.setattr(se, "MIGRATION_MAX_FILES_PER_RUN", 3)

    se.migrate_legacy_metrics(str(worktree))

    assert len(list(legacy_dir.glob("*.jsonl.migrated.*"))) == 3
    assert sum((legacy_dir / name).is_file() for name in names) == 2
    assert list(legacy_dir.glob("*.jsonl.migrating.*")) == []


# PR331 round 3: worktree/root の旧配置は 1 プロセスの移行予算を共有する。
def test_legacy_metrics_migration_shares_budget_across_sources(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)
    payload = b'{"run_id":"legacy"}\n'
    worktree_legacy_dir = Path(se.data_dir(str(worktree))) / "metrics"
    root_legacy_dir = Path(se.data_dir(str(root))) / "metrics"
    worktree_legacy_dir.mkdir(parents=True)
    root_legacy_dir.mkdir(parents=True)
    (worktree_legacy_dir / "a.jsonl").write_bytes(payload)
    root_legacy_path = root_legacy_dir / "b.jsonl"
    root_legacy_path.write_bytes(payload)
    monkeypatch.setattr(se, "MIGRATION_TOTAL_BUDGET_BYTES", len(payload))

    se.migrate_legacy_metrics(str(worktree))

    destination_dir = root / ".claude" / "logs" / "skill-evolution" / "metrics"
    assert (destination_dir / "a.jsonl").read_bytes() == payload
    assert len(list(worktree_legacy_dir.glob("a.jsonl.migrated.*"))) == 1
    assert root_legacy_path.read_bytes() == payload
    assert list(root_legacy_dir.glob("b.jsonl.migrating.*")) == []
    assert list(root_legacy_dir.glob("b.jsonl.migrated.*")) == []


# EV-37 / High: cut 位置がちょうど改行直後（完全レコード先頭）なら読み捨てない。
def test_metrics_migration_keeps_record_starting_exactly_at_cut(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)

    dropped_line = b'{"run_id":"dropped"}\n'
    kept_line_1 = b'{"run_id":"kept-1"}\n'
    kept_line_2 = b'{"run_id":"kept-2"}\n'
    tail = kept_line_1 + kept_line_2
    monkeypatch.setattr(se, "MIGRATION_MAX_BYTES", len(tail))

    legacy_path = Path(se.data_dir(str(worktree))) / "metrics" / "boundary.jsonl"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(dropped_line + tail)

    records = se.read_metrics(str(worktree), "boundary")

    assert [record["run_id"] for record in records] == ["kept-1", "kept-2"]


# EV-37: 複数 helper 呼び出しでも root 解決の git コストは project ごとに 1 回だけ。
def test_root_resolution_is_cached_per_project(monkeypatch, tmp_path) -> None:
    project = _make_project(tmp_path / "project")
    calls = 0

    def _resolve(_project_dir: str, **_kwargs) -> str:
        nonlocal calls
        calls += 1
        return str(project)

    hook_common = se._load_hook_common()
    se._resolve_log_root_cached.cache_clear()
    monkeypatch.setattr(hook_common, "resolve_root_worktree", _resolve)

    se.metrics_path(str(project), "s")
    se.pending_path(str(project), "run")
    se.lock_path(str(project), "s")

    assert calls == 1


# EV-37: storage.logs_dir の traversal は root 配下の既定配置へ戻す。
def test_logs_dir_rejects_traversal(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    _patch_root_worktree(monkeypatch, root)

    resolved = se.logs_dir(
        str(worktree),
        {"storage": {"logs_dir": "../../../outside"}},
    )

    assert resolved == str(root / ".claude" / "logs" / "skill-evolution")
    assert os.path.commonpath([str(root), resolved]) == str(root)


# EV-37 / Critical: symlink 経由で root の外を指す設定は realpath ベースで検出し拒否する。
def test_logs_dir_rejects_traversal_via_symlink(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    outside = tmp_path / "outside"
    outside.mkdir()
    _patch_root_worktree(monkeypatch, root)

    # root 配下に外部を指す symlink を作る
    escape_link = root / "escape"
    os.symlink(outside, escape_link)

    resolved = se.logs_dir(
        str(worktree),
        {"storage": {"logs_dir": "escape/skill-evolution"}},
    )

    assert resolved == str(root / ".claude" / "logs" / "skill-evolution")
    assert not str(Path(resolved).resolve()).startswith(str(outside.resolve()))


# EV-37 / Critical: 既定配置も symlink 脱出する場合は安全な fallback がないため失敗する。
def test_logs_dir_raises_when_default_path_escapes_via_symlink(tmp_path) -> None:
    base = tmp_path / "project"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    os.symlink(outside, base / ".claude")
    config = {"storage": {"logs_dir": se.DEFAULTS["storage"]["logs_dir"]}}

    with pytest.raises(ValueError, match="安全な保存先"):
        se._resolve_logs_subdir(str(base), config)


# EV-37 / Critical: metrics 自体が外部 symlink なら I/O を fail-open で拒否する。
def test_metrics_directory_symlink_escape_skips_write_and_read(monkeypatch, tmp_path) -> None:
    worktree = _make_project(tmp_path / "worktree")
    root = _make_project(tmp_path / "root")
    outside = tmp_path / "outside"
    outside.mkdir()
    _patch_root_worktree(monkeypatch, root)
    logs_base = root / ".claude" / "logs" / "skill-evolution"
    logs_base.mkdir(parents=True)
    os.symlink(outside, logs_base / "metrics")

    se.append_metric(str(worktree), "issue-fix", {"run_id": "blocked"})

    assert list(outside.iterdir()) == []
    assert se.read_metrics(str(worktree), "issue-fix") == []
