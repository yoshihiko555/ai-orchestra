"""proxy_manager.py の単体テスト。

subprocess.Popen / os.kill / socket.connect_ex を mock でテストする。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.module_loader import REPO_ROOT, load_module

# hook_common を先に読み込む
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))

proxy_mgr = load_module(
    "proxy_manager",
    "packages/cocoindex/hooks/proxy_manager.py",
)
# @patch("proxy_manager.xxx") が解決できるよう sys.modules に登録
sys.modules["proxy_manager"] = proxy_mgr

SAMPLE_CONFIG: dict = {
    "enabled": True,
    "server_name": "cocoindex-code",
    "command": "uvx",
    "args": ["--prerelease=explicit", "--with", "cocoindex>=1.0.0a16", "cocoindex-code@latest"],
    "proxy": {
        "enabled": True,
        "port": 8792,
        "port_range": 0,
        "host": "127.0.0.1",
        "pid_file": ".claude/.mcp-proxy.pid",
        "startup_timeout": 10,
    },
}


# =========================================================================
# get_proxy_config
# =========================================================================


class TestGetProxyConfig:
    def test_returns_defaults_when_no_proxy_section(self) -> None:
        result = proxy_mgr.get_proxy_config({})
        assert result["enabled"] is False
        assert result["port"] == 8792
        assert result["host"] == "127.0.0.1"
        assert result["startup_timeout"] == 10

    def test_partial_override(self) -> None:
        config = {"proxy": {"port": 9999}}
        result = proxy_mgr.get_proxy_config(config)
        assert result["port"] == 9999
        assert result["host"] == "127.0.0.1"  # デフォルト維持

    def test_full_override(self) -> None:
        config = {"proxy": {"enabled": True, "port": 5555, "host": "0.0.0.0"}}
        result = proxy_mgr.get_proxy_config(config)
        assert result["enabled"] is True
        assert result["port"] == 5555
        assert result["host"] == "0.0.0.0"

    def test_derives_port_with_project_dir(self) -> None:
        config = {"proxy": {"port": 8792, "port_range": 100}}
        result = proxy_mgr.get_proxy_config(config, project_dir="/home/user/project-a")
        assert 8792 <= result["port"] < 8892

    def test_fixed_port_when_range_zero(self) -> None:
        config = {"proxy": {"port": 9999, "port_range": 0}}
        result = proxy_mgr.get_proxy_config(config, project_dir="/home/user/project-a")
        assert result["port"] == 9999

    def test_no_derivation_without_project_dir(self) -> None:
        config = {"proxy": {"port": 8792, "port_range": 100}}
        result = proxy_mgr.get_proxy_config(config)
        assert result["port"] == 8792  # base port そのまま

    def test_normalizes_project_dir_before_deriving_port(self, tmp_path: Path) -> None:
        config = {"proxy": {"port": 8792, "port_range": 100}}
        real_project = tmp_path / "project"
        real_project.mkdir()
        alias_root = tmp_path / "alias-root"
        alias_root.mkdir()
        alias_project = alias_root / "project-link"
        alias_project.symlink_to(real_project, target_is_directory=True)

        result_real = proxy_mgr.get_proxy_config(config, project_dir=str(real_project))
        result_alias = proxy_mgr.get_proxy_config(config, project_dir=str(alias_project))

        assert result_real["port"] == result_alias["port"]


class TestBuildProxyUrl:
    def test_claude_uses_sse(self) -> None:
        url = proxy_mgr.build_proxy_url("claude", SAMPLE_CONFIG, "/tmp/project")
        assert url.endswith("/sse")

    def test_codex_uses_mcp(self) -> None:
        url = proxy_mgr.build_proxy_url("codex", SAMPLE_CONFIG, "/tmp/project")
        assert url.endswith("/mcp")


class TestStateFiles:
    def test_updates_proxy_state(self, tmp_path: Path) -> None:
        state = proxy_mgr.update_proxy_state(str(tmp_path), SAMPLE_CONFIG, proxy_state="starting")

        assert state["proxy_state"] == "starting"
        assert state["port"] == 8792
        assert os.path.exists(proxy_mgr.resolve_proxy_state_path(str(tmp_path)))

    def test_session_state_round_trip(self, tmp_path: Path) -> None:
        proxy_mgr.write_session_state(
            str(tmp_path),
            "sess-1",
            reconnect_required=True,
            reconnect_notified=False,
        )
        state = proxy_mgr.read_session_state(str(tmp_path), "sess-1")
        assert state["reconnect_required"] is True
        assert state["reconnect_notified"] is False

        proxy_mgr.mark_session_reconnect_notified(str(tmp_path), "sess-1")
        updated = proxy_mgr.read_session_state(str(tmp_path), "sess-1")
        assert updated["reconnect_notified"] is True

        proxy_mgr.clear_session_state(str(tmp_path), "sess-1")
        assert proxy_mgr.read_session_state(str(tmp_path), "sess-1") == {}


# =========================================================================
# _derive_port
# =========================================================================


class TestDerivePort:
    def test_deterministic(self) -> None:
        """同じ project_dir なら常に同じポートを返す。"""
        port_a = proxy_mgr._derive_port("/home/user/project-a", 8792, 100)
        port_b = proxy_mgr._derive_port("/home/user/project-a", 8792, 100)
        assert port_a == port_b

    def test_different_projects_different_ports(self) -> None:
        """異なる project_dir なら異なるポートになる（高確率）。"""
        port_a = proxy_mgr._derive_port("/home/user/project-a", 8792, 100)
        port_b = proxy_mgr._derive_port("/home/user/project-b", 8792, 100)
        # ハッシュ衝突の可能性はあるが、この 2 パスでは異なるはず
        assert port_a != port_b

    def test_port_in_range(self) -> None:
        """導出ポートは base_port 〜 base_port + port_range - 1 の範囲内。"""
        for path in ["/a", "/b", "/c/d/e", "/very/long/path/to/project"]:
            port = proxy_mgr._derive_port(path, 8792, 100)
            assert 8792 <= port < 8892

    def test_range_zero_returns_base(self) -> None:
        assert proxy_mgr._derive_port("/any/path", 9999, 0) == 9999

    def test_range_negative_returns_base(self) -> None:
        assert proxy_mgr._derive_port("/any/path", 9999, -1) == 9999


# =========================================================================
# resolve_pid_path
# =========================================================================


class TestResolvePidPath:
    def test_relative_path(self, tmp_path: Path) -> None:
        result = proxy_mgr.resolve_pid_path(SAMPLE_CONFIG, str(tmp_path))
        expected = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        assert result == expected

    def test_absolute_path(self, tmp_path: Path) -> None:
        abs_pid = "/tmp/test-proxy.pid"
        config = {"proxy": {"pid_file": abs_pid}}
        result = proxy_mgr.resolve_pid_path(config, str(tmp_path))
        assert result == abs_pid


# =========================================================================
# PID ファイル操作
# =========================================================================


class TestPidFileOps:
    def test_write_and_read(self, tmp_path: Path) -> None:
        pid_path = str(tmp_path / "test.pid")
        proxy_mgr._write_pid(pid_path, 12345)
        assert proxy_mgr._read_pid(pid_path) == 12345

    def test_read_nonexistent(self, tmp_path: Path) -> None:
        pid_path = str(tmp_path / "nonexistent.pid")
        assert proxy_mgr._read_pid(pid_path) is None

    def test_remove(self, tmp_path: Path) -> None:
        pid_path = tmp_path / "test.pid"
        pid_path.write_text("12345")
        proxy_mgr._remove_pid(str(pid_path))
        assert not pid_path.exists()

    def test_remove_nonexistent(self, tmp_path: Path) -> None:
        proxy_mgr._remove_pid(str(tmp_path / "nonexistent.pid"))


# =========================================================================
# _is_pid_alive
# =========================================================================


class TestIsPidAlive:
    @patch("proxy_manager.os.kill")
    def test_alive(self, mock_kill: MagicMock) -> None:
        mock_kill.return_value = None
        assert proxy_mgr._is_pid_alive(12345) is True
        mock_kill.assert_called_once_with(12345, 0)

    @patch("proxy_manager.os.kill", side_effect=OSError)
    def test_dead(self, mock_kill: MagicMock) -> None:
        assert proxy_mgr._is_pid_alive(12345) is False


# =========================================================================
# _is_port_in_use
# =========================================================================


class TestIsPortInUse:
    @patch("proxy_manager.socket.socket")
    def test_port_in_use(self, mock_socket_cls: MagicMock) -> None:
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_sock.connect_ex.return_value = 0
        assert proxy_mgr._is_port_in_use("127.0.0.1", 8792) is True

    @patch("proxy_manager.socket.socket")
    def test_port_not_in_use(self, mock_socket_cls: MagicMock) -> None:
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_sock.connect_ex.return_value = 1
        assert proxy_mgr._is_port_in_use("127.0.0.1", 8792) is False


# =========================================================================
# is_proxy_running
# =========================================================================


class TestIsProxyRunning:
    def test_no_pid_file(self, tmp_path: Path) -> None:
        config = {
            **SAMPLE_CONFIG,
            "proxy": {**SAMPLE_CONFIG["proxy"], "pid_file": str(tmp_path / "test.pid")},
        }
        assert proxy_mgr.is_proxy_running(config, str(tmp_path)) is False

    @patch("proxy_manager._is_port_in_use", return_value=True)
    @patch("proxy_manager._is_pid_alive", return_value=True)
    def test_running(self, _alive: MagicMock, _port: MagicMock, tmp_path: Path) -> None:
        pid_path = tmp_path / "test.pid"
        pid_path.write_text("12345")
        config = {**SAMPLE_CONFIG, "proxy": {**SAMPLE_CONFIG["proxy"], "pid_file": str(pid_path)}}
        assert proxy_mgr.is_proxy_running(config, str(tmp_path)) is True
        assert pid_path.exists()

    @patch("proxy_manager._is_pid_alive", return_value=False)
    def test_stale_pid_cleanup(self, _alive: MagicMock, tmp_path: Path) -> None:
        """プロセス死亡時に stale PID ファイルをクリーンアップする。"""
        pid_path = tmp_path / "test.pid"
        pid_path.write_text("99999")
        config = {**SAMPLE_CONFIG, "proxy": {**SAMPLE_CONFIG["proxy"], "pid_file": str(pid_path)}}
        assert proxy_mgr.is_proxy_running(config, str(tmp_path)) is False
        assert not pid_path.exists()

    @patch("proxy_manager._is_port_in_use", return_value=False)
    @patch("proxy_manager._is_pid_alive", return_value=True)
    def test_alive_but_port_not_in_use(
        self, _alive: MagicMock, _port: MagicMock, tmp_path: Path
    ) -> None:
        """プロセスは生きているがポート未使用の場合は False。"""
        pid_path = tmp_path / "test.pid"
        pid_path.write_text("12345")
        config = {**SAMPLE_CONFIG, "proxy": {**SAMPLE_CONFIG["proxy"], "pid_file": str(pid_path)}}
        assert proxy_mgr.is_proxy_running(config, str(tmp_path)) is False


# =========================================================================
# _build_proxy_command
# =========================================================================


class TestBuildProxyCommand:
    def test_builds_command(self) -> None:
        proxy_cfg = proxy_mgr.get_proxy_config(SAMPLE_CONFIG)
        cmd = proxy_mgr._build_proxy_command(SAMPLE_CONFIG, proxy_cfg)
        assert cmd == [
            "mcp-proxy",
            "--pass-environment",
            "--host",
            "127.0.0.1",
            "--port",
            "8792",
            "--",
            "uvx",
            "--prerelease=explicit",
            "--with",
            "cocoindex>=1.0.0a16",
            "cocoindex-code@latest",
        ]

    def test_no_args(self) -> None:
        config = {"command": "my-server"}
        proxy_cfg = {"port": 9999}
        cmd = proxy_mgr._build_proxy_command(config, proxy_cfg)
        assert cmd == [
            "mcp-proxy",
            "--pass-environment",
            "--host",
            "127.0.0.1",
            "--port",
            "9999",
            "--",
            "my-server",
        ]

    def test_missing_command_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="config\\['command'\\] is required"):
            proxy_mgr._build_proxy_command({}, {"port": 8792})


# =========================================================================
# start_proxy
# =========================================================================


class TestStartProxy:
    @patch("proxy_manager._wait_for_port", return_value=True)
    @patch("proxy_manager.subprocess.Popen")
    @patch("proxy_manager.cleanup_orphan")
    @patch("proxy_manager._is_port_in_use", return_value=False)
    @patch("proxy_manager.is_proxy_running", return_value=False)
    def test_normal_start(
        self,
        mock_running: MagicMock,
        mock_port_check: MagicMock,
        mock_cleanup: MagicMock,
        mock_popen: MagicMock,
        mock_wait: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc

        result = proxy_mgr.start_proxy(SAMPLE_CONFIG, str(tmp_path))
        assert result is True

        # PID ファイルが作成されている
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        assert proxy_mgr._read_pid(pid_path) == 99999

    @patch("proxy_manager.is_proxy_running", return_value=True)
    def test_idempotent(self, mock_running: MagicMock, tmp_path: Path) -> None:
        result = proxy_mgr.start_proxy(SAMPLE_CONFIG, str(tmp_path))
        assert result is True

    @patch("proxy_manager._is_port_in_use", return_value=True)
    @patch("proxy_manager.is_proxy_running", return_value=False)
    def test_port_in_use_skips_start(
        self,
        mock_running: MagicMock,
        mock_port_check: MagicMock,
        tmp_path: Path,
    ) -> None:
        """PID ファイルが無効でもポートが使用中なら True を返す。"""
        result = proxy_mgr.start_proxy(SAMPLE_CONFIG, str(tmp_path))
        assert result is True

    @patch("proxy_manager._find_pid_by_port", return_value=77777)
    @patch("proxy_manager._is_port_in_use", return_value=True)
    @patch("proxy_manager.is_proxy_running", return_value=False)
    def test_port_in_use_restores_pid(
        self,
        mock_running: MagicMock,
        mock_port_check: MagicMock,
        mock_find: MagicMock,
        tmp_path: Path,
    ) -> None:
        """ポート使用中で早期リターンする際に実プロセスの PID が復元される。"""
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        proxy_mgr._write_pid(pid_path, 99999)  # stale PID

        result = proxy_mgr.start_proxy(SAMPLE_CONFIG, str(tmp_path))
        assert result is True
        # 実プロセスの PID に書き換えられている
        assert proxy_mgr._read_pid(pid_path) == 77777

    @patch("proxy_manager._find_pid_by_port", return_value=None)
    @patch("proxy_manager._is_port_in_use", return_value=True)
    @patch("proxy_manager.is_proxy_running", return_value=False)
    def test_port_in_use_removes_pid_when_lsof_fails(
        self,
        mock_running: MagicMock,
        mock_port_check: MagicMock,
        mock_find: MagicMock,
        tmp_path: Path,
    ) -> None:
        """lsof で PID を取得できない場合は stale PID ファイルを削除する。"""
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        proxy_mgr._write_pid(pid_path, 99999)

        result = proxy_mgr.start_proxy(SAMPLE_CONFIG, str(tmp_path))
        assert result is True
        assert proxy_mgr._read_pid(pid_path) is None

    @patch("proxy_manager.os.kill")
    @patch("proxy_manager._wait_for_port", return_value=False)
    @patch("proxy_manager.subprocess.Popen")
    @patch("proxy_manager.cleanup_orphan")
    @patch("proxy_manager._is_port_in_use", return_value=False)
    @patch("proxy_manager.is_proxy_running", return_value=False)
    def test_timeout_kills_process(
        self,
        mock_running: MagicMock,
        mock_port_check: MagicMock,
        mock_cleanup: MagicMock,
        mock_popen: MagicMock,
        mock_wait: MagicMock,
        mock_kill: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 88888
        mock_popen.return_value = mock_proc

        result = proxy_mgr.start_proxy(SAMPLE_CONFIG, str(tmp_path))
        assert result is False

        # PID ファイルが削除されている
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        assert proxy_mgr._read_pid(pid_path) is None

    @patch("proxy_manager.subprocess.Popen", side_effect=FileNotFoundError)
    @patch("proxy_manager.cleanup_orphan")
    @patch("proxy_manager._is_port_in_use", return_value=False)
    @patch("proxy_manager.is_proxy_running", return_value=False)
    def test_popen_failure(
        self,
        mock_running: MagicMock,
        mock_port_check: MagicMock,
        mock_cleanup: MagicMock,
        mock_popen: MagicMock,
        tmp_path: Path,
    ) -> None:
        result = proxy_mgr.start_proxy(SAMPLE_CONFIG, str(tmp_path))
        assert result is False


class TestStartProxyBackground:
    @patch("proxy_manager.subprocess.Popen")
    @patch("proxy_manager.get_proxy_state", return_value={"proxy_state": "stopped"})
    def test_launches_helper(
        self,
        mock_state: MagicMock,
        mock_popen: MagicMock,
        tmp_path: Path,
    ) -> None:
        result = proxy_mgr.start_proxy_background(SAMPLE_CONFIG, str(tmp_path))
        assert result is True
        mock_popen.assert_called_once()

        state = json.loads(
            Path(proxy_mgr.resolve_proxy_state_path(str(tmp_path))).read_text(encoding="utf-8")
        )
        assert state["proxy_state"] == "starting"

    @patch("proxy_manager.subprocess.Popen")
    @patch("proxy_manager.get_proxy_state", return_value={"proxy_state": "ready"})
    def test_skips_when_ready(
        self,
        mock_state: MagicMock,
        mock_popen: MagicMock,
        tmp_path: Path,
    ) -> None:
        result = proxy_mgr.start_proxy_background(SAMPLE_CONFIG, str(tmp_path))
        assert result is False
        mock_popen.assert_not_called()


# =========================================================================
# stop_proxy
# =========================================================================


class TestStopProxy:
    @patch("proxy_manager._find_pid_by_port", return_value=None)
    def test_noop_when_no_pid_file(self, mock_find: MagicMock, tmp_path: Path) -> None:
        result = proxy_mgr.stop_proxy(SAMPLE_CONFIG, str(tmp_path))
        assert result is True

    @patch("proxy_manager._wait_for_exit", return_value=True)
    @patch("proxy_manager.os.kill")
    @patch("proxy_manager._is_pid_alive", return_value=True)
    @patch("proxy_manager._find_pid_by_port", return_value=77777)
    def test_stop_via_port_fallback(
        self,
        mock_find: MagicMock,
        mock_alive: MagicMock,
        mock_kill: MagicMock,
        mock_wait: MagicMock,
        tmp_path: Path,
    ) -> None:
        """PID ファイルなしでもポートから PID を発見して停止できる。"""
        result = proxy_mgr.stop_proxy(SAMPLE_CONFIG, str(tmp_path))
        assert result is True
        # ポートから発見した PID に SIGTERM が送られている
        import signal

        mock_kill.assert_any_call(77777, signal.SIGTERM)

    @patch("proxy_manager._is_pid_alive", return_value=False)
    def test_removes_stale_pid(self, mock_alive: MagicMock, tmp_path: Path) -> None:
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        proxy_mgr._write_pid(pid_path, 11111)

        result = proxy_mgr.stop_proxy(SAMPLE_CONFIG, str(tmp_path))
        assert result is True
        assert proxy_mgr._read_pid(pid_path) is None

    @patch("proxy_manager._wait_for_exit", return_value=True)
    @patch("proxy_manager.os.kill")
    @patch("proxy_manager._is_pid_alive", return_value=True)
    def test_sigterm_stop(
        self,
        mock_alive: MagicMock,
        mock_kill: MagicMock,
        mock_wait: MagicMock,
        tmp_path: Path,
    ) -> None:
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        proxy_mgr._write_pid(pid_path, 22222)

        result = proxy_mgr.stop_proxy(SAMPLE_CONFIG, str(tmp_path))
        assert result is True
        assert proxy_mgr._read_pid(pid_path) is None

    @patch("proxy_manager._wait_for_exit", return_value=False)
    @patch("proxy_manager.os.kill")
    @patch("proxy_manager._is_pid_alive", return_value=True)
    def test_sigkill_fallback(
        self,
        mock_alive: MagicMock,
        mock_kill: MagicMock,
        mock_wait: MagicMock,
        tmp_path: Path,
    ) -> None:
        import signal

        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        proxy_mgr._write_pid(pid_path, 33333)

        proxy_mgr.stop_proxy(SAMPLE_CONFIG, str(tmp_path))

        # SIGTERM + SIGKILL が呼ばれている
        kill_signals = [call.args[1] for call in mock_kill.call_args_list]
        assert signal.SIGTERM in kill_signals
        assert signal.SIGKILL in kill_signals


# =========================================================================
# _find_pid_by_port
# =========================================================================


class TestFindPidByPort:
    @patch("proxy_manager.subprocess.run")
    def test_returns_pid(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="12345\n")
        assert proxy_mgr._find_pid_by_port(8792) == 12345

    @patch("proxy_manager.subprocess.run")
    def test_multiple_pids_returns_first(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="11111\n22222\n")
        assert proxy_mgr._find_pid_by_port(8792) == 11111

    @patch("proxy_manager.subprocess.run")
    def test_returns_none_on_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert proxy_mgr._find_pid_by_port(8792) is None

    @patch("proxy_manager.subprocess.run", side_effect=OSError)
    def test_returns_none_on_os_error(self, mock_run: MagicMock) -> None:
        assert proxy_mgr._find_pid_by_port(8792) is None

    @patch(
        "proxy_manager.subprocess.run",
        side_effect=proxy_mgr.subprocess.TimeoutExpired(cmd="lsof", timeout=5),
    )
    def test_returns_none_on_timeout(self, mock_run: MagicMock) -> None:
        assert proxy_mgr._find_pid_by_port(8792) is None


# =========================================================================
# cleanup_orphan
# =========================================================================


class TestCleanupOrphan:
    def test_noop_when_no_pid_file(self, tmp_path: Path) -> None:
        proxy_mgr.cleanup_orphan(SAMPLE_CONFIG, str(tmp_path))

    @patch("proxy_manager._is_pid_alive", return_value=False)
    def test_removes_stale_pid_dead_process(self, mock_alive: MagicMock, tmp_path: Path) -> None:
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        proxy_mgr._write_pid(pid_path, 44444)

        proxy_mgr.cleanup_orphan(SAMPLE_CONFIG, str(tmp_path))
        assert proxy_mgr._read_pid(pid_path) is None

    @patch("proxy_manager._wait_for_exit", return_value=True)
    @patch("proxy_manager.os.kill")
    @patch("proxy_manager._is_pid_alive", return_value=True)
    def test_kills_alive_orphan(
        self,
        mock_alive: MagicMock,
        mock_kill: MagicMock,
        mock_wait: MagicMock,
        tmp_path: Path,
    ) -> None:
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        proxy_mgr._write_pid(pid_path, 55555)

        proxy_mgr.cleanup_orphan(SAMPLE_CONFIG, str(tmp_path))
        assert proxy_mgr._read_pid(pid_path) is None
