"""`orchestra-manager.py uninstall cocoindex` の配布ライフサイクル回帰テスト。

対象観点（docs/evaluation/cocoindex.md, Issue #127 仕様確定）:
- EV-19（should）: `uninstall` は cocoindex 固有の状態（各 CLI 設定ファイルへ書き込み
  済みの MCP エントリ・起動中の mcp-proxy プロセス（PID ファイル / proxy state）・
  セッション state）をクリーンアップの対象外として扱う（意図的な仕様。
  `.claude/rules/cocoindex-usage.md` の「uninstall 時のクリーンアップ」参照）。
  Codex CLI（`.codex/config.toml`）を含む 3 CLI すべての MCP エントリ、および
  稼働中 proxy を模した runtime state（PID ファイル・proxy state ファイル）を
  seed し、uninstall 後も変更されずに残存することを検証する。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.module_loader import REPO_ROOT, load_module

manager_mod = load_module("orchestra_manager_uninstall", "scripts/orchestra-manager.py")
OrchestraManager = manager_mod.OrchestraManager

_CODEX_CONFIG_TOML = """[mcp_servers.cocoindex-code]
command = "uvx"
args = ["cocoindex-code"]
enabled = true
"""


def _make_manager() -> OrchestraManager:
    """実パッケージ定義（packages/cocoindex 含む）を読み込めるよう REPO_ROOT を使う。"""
    return OrchestraManager(REPO_ROOT)


def _seed_project(project_dir: Path) -> dict:
    """cocoindex がインストール済み・proxy 稼働中のプロジェクト状態を seed する。

    稼働中 proxy は、テストプロセス自身の PID（確実に alive）を使って
    PID ファイルと proxy state ファイルを書き出すことでシミュレートする。
    戻り値には uninstall 後の比較に使う seed 内容を含める。
    """
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
    codex_dir = project_dir / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(_CODEX_CONFIG_TOML, encoding="utf-8")

    sessions_dir = project_dir / ".claude" / "state" / "cocoindex-sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "sess-1.json").write_text(
        json.dumps({"reconnect_required": False}), encoding="utf-8"
    )

    # 稼働中 mcp-proxy プロセスをシミュレートする runtime state を seed する。
    # PID には現在のテストプロセス自身の PID（確実に alive）を使う。
    running_pid = os.getpid()
    pid_file = project_dir / ".claude" / ".mcp-proxy.pid"
    pid_file.write_text(str(running_pid), encoding="utf-8")

    proxy_state = {
        "proxy_state": "ready",
        "pid": running_pid,
        "child_pid": None,
        "host": "127.0.0.1",
        "port": 8792,
        "inner_port": None,
        "active_clients": 0,
        "last_disconnect_at": "",
        "last_transition_at": "2026-07-15T00:00:00.000+00:00",
        "last_error": "",
    }
    proxy_state_dir = project_dir / ".claude" / "state"
    proxy_state_dir.mkdir(parents=True, exist_ok=True)
    proxy_state_file = proxy_state_dir / "cocoindex-proxy.json"
    proxy_state_file.write_text(json.dumps(proxy_state), encoding="utf-8")

    return {
        "pid_file": pid_file,
        "pid_content": str(running_pid),
        "proxy_state_file": proxy_state_file,
        "proxy_state_content": proxy_state,
    }


class TestUninstallDoesNotTouchCocoindexRuntimeState:
    def test_mcp_entries_and_proxy_and_session_state_are_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _make_manager()
        _hook_common, proxy_manager = manager._load_proxy_modules()
        stop_proxy_spy = MagicMock()
        monkeypatch.setattr(proxy_manager, "stop_proxy", stop_proxy_spy)

        seed = _seed_project(tmp_path)

        manager.uninstall("cocoindex", str(tmp_path), dry_run=False)

        # 各 CLI（Claude Code / Antigravity / Codex）の MCP エントリは uninstall の
        # 対象外（残存する）
        mcp_data = json.loads((tmp_path / ".mcp.json").read_text())
        assert "cocoindex-code" in mcp_data["mcpServers"]
        settings_data = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
        assert "cocoindex-code" in settings_data["mcpServers"]
        codex_toml = (tmp_path / ".codex" / "config.toml").read_text()
        assert "[mcp_servers.cocoindex-code]" in codex_toml

        # proxy 停止処理は一切呼ばれない
        stop_proxy_spy.assert_not_called()

        # 稼働中 mcp-proxy を模した runtime state（PID ファイル・proxy state ファイル）
        # も uninstall によって書き換え・削除されない
        assert seed["pid_file"].read_text() == seed["pid_content"]
        assert json.loads(seed["proxy_state_file"].read_text()) == seed["proxy_state_content"]

        # セッション state も uninstall の対象外
        session_path = tmp_path / ".claude" / "state" / "cocoindex-sessions" / "sess-1.json"
        assert session_path.exists()

        # パッケージとしては installed_packages から外れる
        orch = json.loads((tmp_path / ".claude" / "orchestra.json").read_text())
        assert "cocoindex" not in orch.get("installed_packages", [])
