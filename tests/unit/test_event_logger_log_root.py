"""event_logger の _resolve_log_root / _resolve_root_worktree テスト。

audit の root 解決は core（hook_common.resolve_root_worktree /
resolve_log_root）へ委譲されている。event_logger 側のラッパーは薄い委譲層に
なったため、ここでは以下の契約のみを検証する:

- `_resolve_root_worktree` は project_dir をそのまま core の
  `resolve_root_worktree` に渡して委譲する（event_logger 独自のロジックは
  持たない）
- `_resolve_log_root` は core の `resolve_log_root` に委譲し、その結果が
  ログパス関数（get_session_log_path / get_log_base_path）に反映される

core 自体の git 実装（2 段解決アルゴリズム）の詳細検証は
`packages/core/tests/`（あれば）または `packages/audit/tests/test_event_logger.py`
の統合テストに委ねる。ここでは実 git サブプロセスに依存しない決定論的な
ユニットテストとして、委譲の配線のみを検証する。
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from tests.module_loader import load_module

mod = load_module("event_logger", "packages/audit/hooks/event_logger.py")

# mod が `from hook_common import resolve_log_root, resolve_root_worktree` を
# 実行した際に sys.modules へ登録された core モジュール本体。
# `_resolve_log_root` は core の `resolve_log_root` に委譲するが、その関数は
# 内部で core モジュール自身のグローバル名前空間にある `resolve_root_worktree`
# を呼び出す（mod 側の名前空間は参照しない）。そのため `_resolve_log_root` の
# 挙動をモックで制御するには mod ではなく core モジュール側の
# `resolve_root_worktree` をパッチする必要がある。
import hook_common

_resolve_log_root = mod._resolve_log_root
_resolve_root_worktree = mod._resolve_root_worktree
get_session_log_path = mod.get_session_log_path
get_log_base_path = mod.get_log_base_path


class TestResolveRootWorktree:
    """`_resolve_root_worktree` が core への単純委譲であることを検証する。"""

    def test_delegates_project_dir_to_core(self, tmp_path: Path) -> None:
        """project_dir を core の resolve_root_worktree にそのまま渡す。"""
        target = str(tmp_path / "project")

        with patch.object(mod, "resolve_root_worktree", return_value=target) as mock_core:
            result = _resolve_root_worktree(target)

        mock_core.assert_called_once_with(target)
        assert result == target

    def test_delegates_none_when_no_project_dir(self) -> None:
        """project_dir 未指定時は core に None をそのまま渡す。"""
        with patch.object(mod, "resolve_root_worktree", return_value=None) as mock_core:
            result = _resolve_root_worktree()

        mock_core.assert_called_once_with(None)
        assert result is None

    def test_returns_none_when_git_fails(self, tmp_path: Path) -> None:
        """git が失敗すると None を返す（core 実装の subprocess 呼び出しを経由）。"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 128
            mock_run.return_value.stdout = ""
            result = _resolve_root_worktree(str(tmp_path))

        assert mock_run.called
        assert result is None

    def test_returns_none_when_git_not_found(self, tmp_path: Path) -> None:
        """git がインストールされていなければ None を返す。"""
        with patch("subprocess.run", side_effect=FileNotFoundError) as mock_run:
            result = _resolve_root_worktree(str(tmp_path))

        assert mock_run.called
        assert result is None


class TestResolveLogRoot:
    def test_uses_root_worktree_when_available(self, tmp_path: Path) -> None:
        """root worktree に .claude/ があればそちらを使う。"""
        root = tmp_path / "root"
        root.mkdir()
        (root / ".claude").mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".claude").mkdir()

        with patch.object(hook_common, "resolve_root_worktree", return_value=str(root)):
            result = _resolve_log_root(str(worktree))

        assert result == str(root)

    def test_falls_back_when_root_has_no_claude_dir(self, tmp_path: Path) -> None:
        """root worktree に .claude/ がなければフォールバック。"""
        root = tmp_path / "root"
        root.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".claude").mkdir()

        with patch.object(hook_common, "resolve_root_worktree", return_value=str(root)):
            result = _resolve_log_root(str(worktree))

        assert result == str(worktree)

    def test_falls_back_when_git_unavailable(self, tmp_path: Path) -> None:
        """git が使えなければ通常の project_dir 解決。"""
        project = tmp_path / "project"
        project.mkdir()
        (project / ".claude").mkdir()

        with patch.object(hook_common, "resolve_root_worktree", return_value=None):
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

        with patch.object(hook_common, "resolve_root_worktree", return_value=str(root)):
            path = get_session_log_path("sess-123", "/some/worktree")

        assert str(root) in path
        assert "sess-123.jsonl" in path

    def test_log_base_path_uses_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        (root / ".claude").mkdir()

        with patch.object(hook_common, "resolve_root_worktree", return_value=str(root)):
            path = get_log_base_path("/some/worktree")

        expected = os.path.join(str(root), ".claude", "logs", "audit")
        assert path == expected
