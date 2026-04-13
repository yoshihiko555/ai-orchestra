"""event_logger の _resolve_log_root / _resolve_root_worktree テスト。"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from tests.module_loader import load_module

mod = load_module("event_logger", "packages/audit/hooks/event_logger.py")
_resolve_log_root = mod._resolve_log_root
_resolve_root_worktree = mod._resolve_root_worktree
get_session_log_path = mod.get_session_log_path
get_log_base_path = mod.get_log_base_path


class TestResolveRootWorktree:
    def test_returns_parent_of_git_common_dir(self, tmp_path: Path) -> None:
        """git rev-parse が成功すると .git の親を返す。"""
        fake_git = tmp_path / "project" / ".git"
        fake_git.mkdir(parents=True)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = f"{fake_git}\n"
            result = _resolve_root_worktree(str(tmp_path / "project"))

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path / "project")
        assert result == str(tmp_path / "project")

    def test_returns_none_when_git_fails(self) -> None:
        """git が失敗すると None を返す。"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 128
            mock_run.return_value.stdout = ""
            result = _resolve_root_worktree()

        assert result is None

    def test_returns_none_when_git_not_found(self) -> None:
        """git がインストールされていなければ None を返す。"""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = _resolve_root_worktree()

        assert result is None

    def test_passes_none_cwd_when_no_project_dir(self) -> None:
        """project_dir 未指定時に cwd=None で git を実行する。"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 128
            mock_run.return_value.stdout = ""
            _resolve_root_worktree()

        assert mock_run.call_args.kwargs["cwd"] is None


class TestResolveLogRoot:
    def test_uses_root_worktree_when_available(self, tmp_path: Path) -> None:
        """root worktree に .claude/ があればそちらを使う。"""
        root = tmp_path / "root"
        root.mkdir()
        (root / ".claude").mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".claude").mkdir()

        with patch.object(mod, "_resolve_root_worktree", return_value=str(root)):
            result = _resolve_log_root(str(worktree))

        assert result == str(root)

    def test_falls_back_when_root_has_no_claude_dir(self, tmp_path: Path) -> None:
        """root worktree に .claude/ がなければフォールバック。"""
        root = tmp_path / "root"
        root.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".claude").mkdir()

        with patch.object(mod, "_resolve_root_worktree", return_value=str(root)):
            result = _resolve_log_root(str(worktree))

        assert result == str(worktree)

    def test_falls_back_when_git_unavailable(self, tmp_path: Path) -> None:
        """git が使えなければ通常の project_dir 解決。"""
        project = tmp_path / "project"
        project.mkdir()
        (project / ".claude").mkdir()

        with patch.object(mod, "_resolve_root_worktree", return_value=None):
            result = _resolve_log_root(str(project))

        assert result == str(project)


class TestLogPathsUseLogRoot:
    """ログパス関数が _resolve_log_root 経由で root worktree を使うことを検証。"""

    def setup_method(self) -> None:
        pass  # no cache to clear

    def test_session_log_path_uses_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        (root / ".claude").mkdir()

        with patch.object(mod, "_resolve_root_worktree", return_value=str(root)):
            path = get_session_log_path("sess-123", "/some/worktree")

        assert str(root) in path
        assert "sess-123.jsonl" in path

    def test_log_base_path_uses_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        (root / ".claude").mkdir()

        with patch.object(mod, "_resolve_root_worktree", return_value=str(root)):
            path = get_log_base_path("/some/worktree")

        expected = os.path.join(str(root), ".claude", "logs", "audit")
        assert path == expected
