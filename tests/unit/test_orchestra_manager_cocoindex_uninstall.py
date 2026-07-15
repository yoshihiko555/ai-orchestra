"""`orchestra-manager.py uninstall cocoindex` の配布ライフサイクル回帰テスト。

対象観点（docs/evaluation/cocoindex.md, Issue #127 仕様確定）:
- EV-19（should）: `uninstall` は cocoindex 固有の状態（各 CLI 設定ファイルへ書き込み
  済みの MCP エントリ・起動中の mcp-proxy プロセス・セッション state）をクリーンアップ
  の対象外として扱う（意図的な仕様。`.claude/rules/cocoindex-usage.md` の
  「uninstall 時のクリーンアップ」参照）。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.module_loader import REPO_ROOT, load_module

manager_mod = load_module("orchestra_manager_uninstall", "scripts/orchestra-manager.py")
OrchestraManager = manager_mod.OrchestraManager


def _make_manager() -> OrchestraManager:
    """実パッケージ定義（packages/cocoindex 含む）を読み込めるよう REPO_ROOT を使う。"""
    return OrchestraManager(REPO_ROOT)


def _seed_project(project_dir: Path) -> None:
    (project_dir / ".claude").mkdir(parents=True)
    (project_dir / ".claude" / "orchestra.json").write_text(
        json.dumps({"installed_packages": ["cocoindex"], "file_hashes": {}}),
        encoding="utf-8",
    )
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"cocoindex-code": {"command": "uvx"}}}),
        encoding="utf-8",
    )
    gemini_dir = project_dir / ".gemini"
    gemini_dir.mkdir()
    (gemini_dir / "settings.json").write_text(
        json.dumps({"mcpServers": {"cocoindex-code": {"command": "uvx"}}}),
        encoding="utf-8",
    )
    sessions_dir = project_dir / ".claude" / "state" / "cocoindex-sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "sess-1.json").write_text(
        json.dumps({"reconnect_required": False}), encoding="utf-8"
    )


class TestUninstallDoesNotTouchCocoindexRuntimeState:
    def test_mcp_entries_and_proxy_and_session_state_are_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _make_manager()
        _hook_common, proxy_manager = manager._load_proxy_modules()
        stop_proxy_spy = MagicMock()
        monkeypatch.setattr(proxy_manager, "stop_proxy", stop_proxy_spy)

        _seed_project(tmp_path)

        manager.uninstall("cocoindex", str(tmp_path), dry_run=False)

        # 各 CLI の MCP エントリは uninstall の対象外（残存する）
        mcp_data = json.loads((tmp_path / ".mcp.json").read_text())
        assert "cocoindex-code" in mcp_data["mcpServers"]
        settings_data = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
        assert "cocoindex-code" in settings_data["mcpServers"]

        # proxy 停止処理は一切呼ばれない
        stop_proxy_spy.assert_not_called()

        # セッション state も uninstall の対象外
        session_path = tmp_path / ".claude" / "state" / "cocoindex-sessions" / "sess-1.json"
        assert session_path.exists()

        # パッケージとしては installed_packages から外れる
        orch = json.loads((tmp_path / ".claude" / "orchestra.json").read_text())
        assert "cocoindex" not in orch.get("installed_packages", [])
