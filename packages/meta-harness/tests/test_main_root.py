"""メインルート解決のテスト（Sec2-0, EV-32, EV-33）。

- feature worktree 内から実行してもメイン worktree ルートに解決されること（EV-32）
- `storage.root` 上書きが git 解決より優先されること（EV-32）
- bare repo など main root が導出できない環境で fail-closed すること（EV-33）
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_main_root",
    "packages/meta-harness/lib/meta_harness_common.py",
)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _add_feature_worktree(git_project: Path, name: str = "feat-x") -> Path:
    worktrees_root = git_project / ".worktrees"
    worktrees_root.mkdir(parents=True, exist_ok=True)
    worktree_dir = worktrees_root / name
    _git("worktree", "add", "--detach", str(worktree_dir), "HEAD", cwd=git_project)
    return worktree_dir


class TestMainRootFromWorktree:
    # EV-32
    def test_init_from_feature_worktree_resolves_store_to_main_root(
        self, git_project: Path, run_meta
    ) -> None:
        worktree_dir = _add_feature_worktree(git_project)

        result = run_meta("init", "--json", project=worktree_dir, check=True)
        payload = json.loads(result.stdout)

        assert Path(payload["main_root"]).resolve() == git_project.resolve()
        assert (git_project / ".claude" / "meta-harness" / "ledger.jsonl").is_file()
        # feature worktree 自身の直下には store が作られない
        assert not (worktree_dir / ".claude" / "meta-harness").exists()

    # EV-32
    def test_register_from_feature_worktree_shares_single_store(
        self, git_project: Path, tmp_path: Path, run_meta, default_overlay
    ) -> None:
        worktree_dir = _add_feature_worktree(git_project)
        run_meta("init", project=git_project, check=True)

        overlay_dir = default_overlay(tmp_path)
        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            "--description",
            "from feature worktree",
            "--json",
            project=worktree_dir,
            check=True,
        )
        payload = json.loads(result.stdout)
        cand_id = payload["cand_id"]

        # 候補は main root 側の candidates/ に現れる（worktree 側には作られない）
        assert (git_project / ".claude" / "meta-harness" / "candidates" / cand_id).is_dir()
        assert not (worktree_dir / ".claude" / "meta-harness").exists()


class TestStorageRootOverride:
    # EV-32
    def test_storage_root_override_takes_precedence_over_git_resolution(
        self, git_project: Path, tmp_path: Path, run_meta
    ) -> None:
        override_root = tmp_path / "explicit-root"
        override_root.mkdir()
        local_config_dir = git_project / ".claude" / "config" / "meta-harness"
        local_config_dir.mkdir(parents=True, exist_ok=True)
        (local_config_dir / "meta-harness.local.yaml").write_text(
            f"storage:\n  root: {override_root}\n", encoding="utf-8"
        )

        result = run_meta("init", "--json", project=git_project, check=True)
        payload = json.loads(result.stdout)

        assert Path(payload["main_root"]) == override_root
        assert (override_root / ".claude" / "meta-harness" / "ledger.jsonl").is_file()
        # git 解決先（git_project 自身）には store が作られない
        assert not (git_project / ".claude" / "meta-harness").exists()

    def test_resolve_main_root_relative_storage_root_is_rejected(self, tmp_path: Path) -> None:
        config = {"storage": {"root": "relative/path"}}
        try:
            mh.resolve_main_root(tmp_path, config)
        except mh.MetaHarnessRootError:
            pass
        else:
            raise AssertionError("relative storage.root should raise MetaHarnessRootError")

    def test_store_dir_rejects_absolute_path(self, tmp_path: Path) -> None:
        config = {"storage": {"dir": "/tmp/absolute-not-allowed"}}
        try:
            mh.store_dir(tmp_path, config)
        except mh.MetaHarnessRootError:
            pass
        else:
            raise AssertionError("absolute storage.dir should raise MetaHarnessRootError")

    def test_cli_exits_nonzero_when_storage_dir_is_absolute(
        self, git_project: Path, run_meta
    ) -> None:
        local_config_dir = git_project / ".claude" / "config" / "meta-harness"
        local_config_dir.mkdir(parents=True, exist_ok=True)
        (local_config_dir / "meta-harness.local.yaml").write_text(
            "storage:\n  dir: /some/absolute/path\n", encoding="utf-8"
        )

        result = run_meta("init", project=git_project, check=False)

        assert result.returncode == 2
        assert "absolute" in result.stderr.lower()


class TestBareRepoFailClosed:
    # EV-33
    def test_resolve_main_root_raises_for_bare_repo(self, tmp_path: Path) -> None:
        bare_dir = tmp_path / "bare.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", str(bare_dir)], check=True, capture_output=True
        )

        try:
            mh.resolve_main_root(bare_dir, {"storage": {"root": None}})
        except mh.MetaHarnessRootError:
            pass
        else:
            raise AssertionError("bare repo should raise MetaHarnessRootError")

    # EV-33
    def test_cli_exits_2_for_bare_repo_without_storage_root(self, tmp_path: Path, run_meta) -> None:
        bare_dir = tmp_path / "bare.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", str(bare_dir)], check=True, capture_output=True
        )

        result = run_meta("init", project=bare_dir, check=False)

        assert result.returncode == 2
        assert "error" in result.stderr.lower()

    # EV-33
    def test_bare_repo_with_storage_root_override_succeeds(self, tmp_path: Path, run_meta) -> None:
        bare_dir = tmp_path / "bare.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", str(bare_dir)], check=True, capture_output=True
        )
        override_root = tmp_path / "explicit-root-for-bare"
        override_root.mkdir()
        local_config_dir = bare_dir / ".claude" / "config" / "meta-harness"
        local_config_dir.mkdir(parents=True, exist_ok=True)
        (local_config_dir / "meta-harness.local.yaml").write_text(
            f"storage:\n  root: {override_root}\n", encoding="utf-8"
        )

        result = run_meta("init", project=bare_dir, check=True)

        assert result.returncode == 0
        assert (override_root / ".claude" / "meta-harness" / "ledger.jsonl").is_file()
