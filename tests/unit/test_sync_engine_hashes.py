"""sync_engine.py の配布時ハッシュ記録ユーティリティのテスト。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# sync_engine は scripts/ からの相対 import を使うため sys.path にスクリプトルートを追加
_repo_root = Path(__file__).resolve().parents[2]
_scripts_dir = str(_repo_root / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from tests.module_loader import load_module

sync_engine = load_module("sync_engine", "scripts/lib/sync_engine.py")


class TestComputeFileHash:
    """compute_file_hash のテスト。"""

    def test_matches_hashlib_sha256(self, tmp_path: Path) -> None:
        """既知バイト列の SHA-256 と一致する。"""
        path = tmp_path / "file.txt"
        path.write_bytes(b"hello world")

        result = sync_engine.compute_file_hash(path)

        assert result == hashlib.sha256(b"hello world").hexdigest()

    def test_different_content_different_hash(self, tmp_path: Path) -> None:
        """内容が異なればハッシュも異なる。"""
        path_a = tmp_path / "a.txt"
        path_a.write_bytes(b"content-a")
        path_b = tmp_path / "b.txt"
        path_b.write_bytes(b"content-b")

        assert sync_engine.compute_file_hash(path_a) != sync_engine.compute_file_hash(path_b)


class TestRecordAndGetFileHash:
    """record_file_hash / get_recorded_file_hash のテスト。"""

    def test_round_trip(self) -> None:
        """record したハッシュを get で取得できる。"""
        orch: dict = {}
        sync_engine.record_file_hash(orch, "mypkg", "agents/foo.md", "abc123")

        assert sync_engine.get_recorded_file_hash(orch, "mypkg", "agents/foo.md") == "abc123"

    def test_multiple_packages_isolated(self) -> None:
        """パッケージ毎にハッシュが分離される。"""
        orch: dict = {}
        sync_engine.record_file_hash(orch, "pkg-a", "config/pkg-a/x.yaml", "hash-a")
        sync_engine.record_file_hash(orch, "pkg-b", "config/pkg-b/x.yaml", "hash-b")

        assert sync_engine.get_recorded_file_hash(orch, "pkg-a", "config/pkg-a/x.yaml") == "hash-a"
        assert sync_engine.get_recorded_file_hash(orch, "pkg-b", "config/pkg-b/x.yaml") == "hash-b"

    def test_overwrite_updates_hash(self) -> None:
        """同じキーに再度 record すると上書きされる。"""
        orch: dict = {}
        sync_engine.record_file_hash(orch, "mypkg", "agents/foo.md", "old-hash")
        sync_engine.record_file_hash(orch, "mypkg", "agents/foo.md", "new-hash")

        assert sync_engine.get_recorded_file_hash(orch, "mypkg", "agents/foo.md") == "new-hash"

    def test_missing_file_hashes_key_returns_none(self) -> None:
        """file_hashes キー自体が存在しない旧形式の orchestra.json では None を返す（後方互換）。"""
        orch: dict = {"installed_packages": ["mypkg"]}

        assert sync_engine.get_recorded_file_hash(orch, "mypkg", "agents/foo.md") is None

    def test_missing_package_returns_none(self) -> None:
        """未記録のパッケージ名は None を返す。"""
        orch: dict = {"file_hashes": {"other-pkg": {"agents/foo.md": "hash"}}}

        assert sync_engine.get_recorded_file_hash(orch, "mypkg", "agents/foo.md") is None

    def test_missing_file_key_returns_none(self) -> None:
        """未記録のファイルキーは None を返す。"""
        orch: dict = {"file_hashes": {"mypkg": {"agents/foo.md": "hash"}}}

        assert sync_engine.get_recorded_file_hash(orch, "mypkg", "agents/bar.md") is None


class TestIsUserModified:
    """is_user_modified のテスト（Issue #241: agents 再同期ハッシュガード）。"""

    def test_dst_missing_returns_false(self, tmp_path: Path) -> None:
        """dst が存在しない場合は False（保護対象なし、通常どおり同期させる）。"""
        orch: dict = {"file_hashes": {"mypkg": {"agents/foo.md": "hash"}}}
        dst = tmp_path / "agents" / "foo.md"

        assert sync_engine.is_user_modified(orch, "mypkg", "agents/foo.md", dst) is False

    def test_no_recorded_hash_returns_false(self, tmp_path: Path) -> None:
        """ハッシュ未記録（旧形式 orchestra.json 含む）の場合は False（後方互換で上書きを許可）。"""
        orch: dict = {"file_hashes": {}}
        dst = tmp_path / "agents" / "foo.md"
        dst.parent.mkdir(parents=True)
        dst.write_text("anything", encoding="utf-8")

        assert sync_engine.is_user_modified(orch, "mypkg", "agents/foo.md", dst) is False

    def test_hash_mismatch_returns_true(self, tmp_path: Path) -> None:
        """現在の内容が配布時ハッシュと異なれば True（ユーザー編集済み）。"""
        dst = tmp_path / "agents" / "foo.md"
        dst.parent.mkdir(parents=True)
        dst.write_text("user edited", encoding="utf-8")
        orch: dict = {
            "file_hashes": {"mypkg": {"agents/foo.md": hashlib.sha256(b"distributed").hexdigest()}}
        }

        assert sync_engine.is_user_modified(orch, "mypkg", "agents/foo.md", dst) is True

    def test_hash_match_returns_false(self, tmp_path: Path) -> None:
        """現在の内容が配布時ハッシュと一致すれば False（未編集）。"""
        dst = tmp_path / "agents" / "foo.md"
        dst.parent.mkdir(parents=True)
        dst.write_text("distributed", encoding="utf-8")
        orch: dict = {
            "file_hashes": {"mypkg": {"agents/foo.md": hashlib.sha256(b"distributed").hexdigest()}}
        }

        assert sync_engine.is_user_modified(orch, "mypkg", "agents/foo.md", dst) is False


class TestCollectManagedAgentStems:
    """collect_managed_agent_stems のテスト。"""

    def test_collects_stems_from_single_package(self, tmp_path: Path) -> None:
        """1 パッケージの manifest.agents から stem 集合を収集する。"""
        pkg_dir = tmp_path / "packages" / "agent-routing"
        pkg_dir.mkdir(parents=True)
        manifest = {
            "name": "agent-routing",
            "agents": ["agents/planner.md", "agents/architect.md"],
        }
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        result = sync_engine.collect_managed_agent_stems(tmp_path, ["agent-routing"])

        assert result == {"planner", "architect"}

    def test_collects_stems_across_multiple_packages(self, tmp_path: Path) -> None:
        """複数パッケージの manifest.agents を合算する。"""
        for name, agents in (
            ("pkg-a", ["agents/foo.md"]),
            ("pkg-b", ["agents/bar.md", "agents/baz.md"]),
        ):
            pkg_dir = tmp_path / "packages" / name
            pkg_dir.mkdir(parents=True)
            manifest = {"name": name, "agents": agents}
            (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        result = sync_engine.collect_managed_agent_stems(tmp_path, ["pkg-a", "pkg-b"])

        assert result == {"foo", "bar", "baz"}

    def test_ignores_uninstalled_packages(self, tmp_path: Path) -> None:
        """installed_packages に含まれないパッケージは無視する。"""
        pkg_dir = tmp_path / "packages" / "not-installed"
        pkg_dir.mkdir(parents=True)
        manifest = {"name": "not-installed", "agents": ["agents/foo.md"]}
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        result = sync_engine.collect_managed_agent_stems(tmp_path, [])

        assert result == set()

    def test_missing_manifest_skipped(self, tmp_path: Path) -> None:
        """manifest.json がないパッケージはスキップする。"""
        result = sync_engine.collect_managed_agent_stems(tmp_path, ["no-manifest"])

        assert result == set()

    def test_invalid_manifest_skipped(self, tmp_path: Path) -> None:
        """壊れた manifest.json はスキップする。"""
        pkg_dir = tmp_path / "packages" / "broken"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "manifest.json").write_text("invalid json", encoding="utf-8")

        result = sync_engine.collect_managed_agent_stems(tmp_path, ["broken"])

        assert result == set()

    def test_no_agents_field(self, tmp_path: Path) -> None:
        """agents フィールドがない manifest は空集合として扱う。"""
        pkg_dir = tmp_path / "packages" / "no-agents"
        pkg_dir.mkdir(parents=True)
        manifest = {"name": "no-agents"}
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        result = sync_engine.collect_managed_agent_stems(tmp_path, ["no-agents"])

        assert result == set()


class TestOrchestraJsonLedgerConsistency:
    """code L4: この worktree 自身の `.claude/orchestra.json` 台帳が、実際にコミットされた同期
    済みファイル実体のハッシュと一致していることを検算する。

    `record_file_hash()`（`sync_engine.sync_packages()` 経由）は同期先ファイル
    (`.claude/<rel>` / `codex_file_hashes` は project root 直下の `<rel>`) の
    `compute_file_hash()` を記録する契約なので、ここでは逆方向に「台帳の値」と「ディスク上の
    実ファイルの実際のハッシュ」を突合する。install/uninstall の sync コードはこの台帳を
    distributed-content baseline として信頼するため、ズレたまま放置すると次回 sync で誤って
    「変更なし」判定される（PR #210 8巡目レビュー: `config/loop-harness/loops/issue-loop.yaml`
    が旧ダイジェストのまま放置されていた）。
    """

    def _load_orchestra_json(self) -> dict:
        path = _repo_root / ".claude" / "orchestra.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_file_hashes_match_committed_synced_files(self) -> None:
        """`file_hashes[pkg][rel]` は `.claude/<rel>` の実際の sha256 と一致する。"""
        orch = self._load_orchestra_json()
        mismatches = []
        for pkg_name, files in orch.get("file_hashes", {}).items():
            for rel_path, recorded_hash in files.items():
                target = _repo_root / ".claude" / rel_path
                assert target.is_file(), f"{pkg_name}/{rel_path}: synced file missing on disk"
                actual_hash = sync_engine.compute_file_hash(target)
                if actual_hash != recorded_hash:
                    mismatches.append((pkg_name, rel_path, recorded_hash, actual_hash))

        assert not mismatches, (
            "orchestra.json file_hashes ledger is stale for: "
            f"{mismatches} (re-run `python scripts/orchestra-manager.py context sync` "
            "or update the ledger to match the committed file)"
        )

    def test_codex_file_hashes_match_committed_synced_files(self) -> None:
        """`codex_file_hashes[rel]` は project root 直下の `<rel>` の実際の sha256 と一致する。"""
        orch = self._load_orchestra_json()
        mismatches = []
        for rel_path, recorded_hash in orch.get("codex_file_hashes", {}).items():
            target = _repo_root / rel_path
            assert target.is_file(), f"{rel_path}: codex synced file missing on disk"
            actual_hash = sync_engine.compute_file_hash(target)
            if actual_hash != recorded_hash:
                mismatches.append((rel_path, recorded_hash, actual_hash))

        assert not mismatches, f"orchestra.json codex_file_hashes ledger is stale for: {mismatches}"
