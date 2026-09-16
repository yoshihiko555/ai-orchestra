"""proxy_manager.py のユニットテスト。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.module_loader import load_module

proxy_mgr = load_module("proxy_manager", "packages/cocoindex/hooks/proxy_manager.py")


class TestDerivePort:
    """_derive_port のテスト。"""

    def test_deterministic(self):
        """同じ入力で同じポートを返す。"""
        port1 = proxy_mgr._derive_port("/project/a", 8792, 100)
        port2 = proxy_mgr._derive_port("/project/a", 8792, 100)
        assert port1 == port2

    def test_different_projects_different_ports(self):
        """異なるプロジェクトで異なるポートを返す（このパスの組では確定）。"""
        port1 = proxy_mgr._derive_port("/project/a", 8792, 100)
        port2 = proxy_mgr._derive_port("/project/b", 8792, 100)
        assert port1 != port2

    def test_port_in_range(self):
        """導出ポートが base_port + port_range 内にある。"""
        port = proxy_mgr._derive_port("/test/project", 9000, 50)
        assert 9000 <= port < 9050

    def test_fixed_mode(self):
        """port_range <= 0 の場合、base_port をそのまま返す。"""
        assert proxy_mgr._derive_port("/project", 8792, 0) == 8792
        assert proxy_mgr._derive_port("/project", 8792, -1) == 8792


class TestGetProxyConfig:
    """get_proxy_config のテスト。"""

    def test_defaults_applied(self):
        """デフォルト値が適用される。"""
        result = proxy_mgr.get_proxy_config({})
        assert result["enabled"] is False
        assert result["port"] == 8792
        assert result["host"] == "127.0.0.1"

    def test_custom_values(self):
        """カスタム値が上書きされる。"""
        config = {"proxy": {"enabled": True, "port": 9000}}
        result = proxy_mgr.get_proxy_config(config)
        assert result["enabled"] is True
        assert result["port"] == 9000
        assert result["host"] == "127.0.0.1"  # 未指定キーはデフォルト維持

        full_config = {"proxy": {"enabled": True, "port": 5555, "host": "0.0.0.0"}}
        full_result = proxy_mgr.get_proxy_config(full_config)
        assert full_result["host"] == "0.0.0.0"  # host も上書きされる

    def test_project_dir_derives_port(self):
        """project_dir が指定されるとポートが導出される。"""
        result = proxy_mgr.get_proxy_config({}, project_dir="/my/project")
        assert 8792 <= result["port"] < 8892

    def test_fixed_port_when_range_zero(self):
        """port_range=0 のとき project_dir があってもポート導出しない。"""
        config = {"proxy": {"port": 9999, "port_range": 0}}
        result = proxy_mgr.get_proxy_config(config, project_dir="/home/user/project-a")
        assert result["port"] == 9999

    def test_no_derivation_without_project_dir(self):
        """project_dir 未指定時はポートを導出しない。"""
        config = {"proxy": {"port": 8792, "port_range": 100}}
        result = proxy_mgr.get_proxy_config(config)
        assert result["port"] == 8792

    def test_project_dir_alias_uses_same_port(self, tmp_path: Path):
        """同じ実体への別パスでも同じポートを使う。"""
        real_project = tmp_path / "project"
        real_project.mkdir()
        alias_root = tmp_path / "alias-root"
        alias_root.mkdir()
        alias_project = alias_root / "project-link"
        alias_project.symlink_to(real_project, target_is_directory=True)

        result_real = proxy_mgr.get_proxy_config({}, project_dir=str(real_project))
        result_alias = proxy_mgr.get_proxy_config({}, project_dir=str(alias_project))

        assert result_real["port"] == result_alias["port"]


class TestBuildProxyUrl:
    def test_claude_uses_sse(self):
        url = proxy_mgr.build_proxy_url(
            "claude", {"proxy": {"port": 8792, "port_range": 0}}, "/tmp"
        )
        assert url == "http://127.0.0.1:8792/sse"

    def test_codex_uses_mcp(self):
        url = proxy_mgr.build_proxy_url("codex", {"proxy": {"port": 8792, "port_range": 0}}, "/tmp")
        assert url == "http://127.0.0.1:8792/mcp"


class TestBuildSupervisorCommand:
    def test_builds_command(self):
        cmd = proxy_mgr._build_supervisor_command("/tmp/project")
        assert cmd[0] == "python3"
        assert cmd[1].endswith("proxy_supervisor.py")
        assert cmd[2] == "/tmp/project"


class TestStateFiles:
    def test_resolve_paths(self, tmp_path):
        proxy_path = proxy_mgr.resolve_proxy_state_path(str(tmp_path))
        session_path = proxy_mgr.resolve_session_state_path(str(tmp_path), "sess-1")

        assert proxy_path.endswith(".claude/state/cocoindex-proxy.json")
        assert session_path.endswith(".claude/state/cocoindex-sessions/sess-1.json")

    def test_proxy_state_round_trip(self, tmp_path):
        state = proxy_mgr.update_proxy_state(
            str(tmp_path),
            {"proxy": {"port": 8792, "port_range": 0}},
            proxy_state="starting",
        )
        assert state["proxy_state"] == "starting"

        saved = proxy_mgr.read_proxy_state(str(tmp_path))
        assert saved["proxy_state"] == "starting"
        assert saved["port"] == 8792

    def test_session_state_round_trip(self, tmp_path):
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


class TestResolvePidPath:
    """resolve_pid_path のテスト。"""

    def test_relative_path(self):
        """相対パスを project_dir で解決する。"""
        result = proxy_mgr.resolve_pid_path({}, "/my/project")
        assert result == "/my/project/.claude/.mcp-proxy.pid"

    def test_absolute_path(self):
        """絶対パスはそのまま使う。"""
        config = {"proxy": {"pid_file": "/tmp/proxy.pid"}}
        result = proxy_mgr.resolve_pid_path(config, "/my/project")
        assert result == "/tmp/proxy.pid"


class TestReadWritePid:
    """PID ファイルの読み書きテスト。"""

    def test_write_and_read(self, tmp_path):
        """PID を書き出して読み取れる。"""
        pid_path = str(tmp_path / "test.pid")
        proxy_mgr._write_pid(pid_path, 12345)
        result = proxy_mgr._read_pid(pid_path)
        assert result == 12345

    def test_read_nonexistent(self, tmp_path):
        """存在しないファイルは None を返す。"""
        result = proxy_mgr._read_pid(str(tmp_path / "missing.pid"))
        assert result is None

    def test_read_invalid_content(self, tmp_path):
        """不正な内容は None を返す。"""
        pid_path = tmp_path / "bad.pid"
        pid_path.write_text("not a number")
        result = proxy_mgr._read_pid(str(pid_path))
        assert result is None

    def test_read_zero_pid(self, tmp_path):
        """PID 0 は None を返す。"""
        pid_path = tmp_path / "zero.pid"
        pid_path.write_text("0")
        result = proxy_mgr._read_pid(str(pid_path))
        assert result is None


class TestRemovePid:
    """_remove_pid のテスト。"""

    def test_removes_existing(self, tmp_path):
        """既存のPIDファイルを削除する。"""
        pid_path = tmp_path / "test.pid"
        pid_path.write_text("123")
        proxy_mgr._remove_pid(str(pid_path))
        assert not pid_path.exists()

    def test_nonexistent_no_error(self, tmp_path):
        """存在しないファイルでもエラーにならない。"""
        proxy_mgr._remove_pid(str(tmp_path / "missing.pid"))  # 例外なし


class TestIsProxyRunning:
    """is_proxy_running のテスト。"""

    def test_no_pid_file(self, tmp_path):
        """PID ファイルがない場合、False。"""
        result = proxy_mgr.is_proxy_running({}, str(tmp_path))
        assert result is False

    def test_pid_alive_and_port_in_use(self, tmp_path):
        """PID が生存中かつポート使用中の場合、True。"""
        pid_path = tmp_path / ".claude" / ".mcp-proxy.pid"
        pid_path.parent.mkdir(parents=True)
        pid_path.write_text(str(os.getpid()))

        with patch.object(proxy_mgr, "_is_port_in_use", return_value=True):
            result = proxy_mgr.is_proxy_running({}, str(tmp_path))
        assert result is True

    def test_pid_dead_cleans_up(self, tmp_path):
        """PID が死亡の場合、PID ファイルを削除して False。"""
        pid_path = tmp_path / ".claude" / ".mcp-proxy.pid"
        pid_path.parent.mkdir(parents=True)
        pid_path.write_text("99999999")  # 存在しない PID

        with patch.object(proxy_mgr, "_is_pid_alive", return_value=False):
            result = proxy_mgr.is_proxy_running({}, str(tmp_path))
        assert result is False
        assert not pid_path.exists()

    def test_pid_alive_but_port_not_in_use(self, tmp_path):
        """PID は生きているがポートが未使用の場合、False。"""
        pid_path = tmp_path / ".claude" / ".mcp-proxy.pid"
        pid_path.parent.mkdir(parents=True)
        pid_path.write_text(str(os.getpid()))

        with patch.object(proxy_mgr, "_is_port_in_use", return_value=False):
            result = proxy_mgr.is_proxy_running({}, str(tmp_path))
        assert result is False

    def test_idle_state_counts_as_running(self, tmp_path):
        proxy_mgr.update_proxy_state(
            str(tmp_path),
            {"proxy": {"port": 8792, "port_range": 0}},
            proxy_state="idle",
            pid=12345,
            child_pid=54321,
            inner_port=9999,
            active_clients=0,
            last_disconnect_at="2026-04-23T00:00:00+00:00",
        )

        with patch.object(proxy_mgr, "_is_port_in_use", return_value=True):
            result = proxy_mgr.is_proxy_running(
                {"proxy": {"port": 8792, "port_range": 0}}, str(tmp_path)
            )
        assert result is True


class TestIsProxyPortFree:
    """is_proxy_port_free のテスト。"""

    def test_returns_true_when_port_is_free(self, tmp_path):
        config = {"proxy": {"port": 8792, "port_range": 0}}
        with patch.object(proxy_mgr, "_is_port_in_use", return_value=False):
            result = proxy_mgr.is_proxy_port_free(config, str(tmp_path))
        assert result is True

    def test_returns_false_when_port_is_in_use(self, tmp_path):
        config = {"proxy": {"port": 8792, "port_range": 0}}
        with patch.object(proxy_mgr, "_is_port_in_use", return_value=True):
            result = proxy_mgr.is_proxy_port_free(config, str(tmp_path))
        assert result is False


class TestStartProxy:
    """start_proxy のテスト。"""

    def test_already_running_skips(self, tmp_path):
        """既に起動中の場合、スキップして True。"""
        with patch.object(proxy_mgr, "is_proxy_running", return_value=True):
            result = proxy_mgr.start_proxy({}, str(tmp_path))
        assert result is True

    def test_port_in_use_recovers(self, tmp_path):
        """ポートが使用中で所有プロセスが mcp-proxy と検証できた場合、PID を復元して True。"""
        with (
            patch.object(proxy_mgr, "is_proxy_running", return_value=False),
            patch.object(proxy_mgr, "_is_port_in_use", return_value=True),
            patch.object(proxy_mgr, "_find_pid_by_port", return_value=12345),
            patch.object(proxy_mgr, "_looks_like_mcp_proxy", return_value=True),
            patch.object(proxy_mgr, "_write_pid") as mock_write,
        ):
            result = proxy_mgr.start_proxy({"command": "test"}, str(tmp_path))
        assert result is True
        mock_write.assert_called_once()

    def test_port_in_use_by_unverified_process_fails(self, tmp_path):
        """ポート占有プロセスが mcp-proxy と検証できない場合は乗っ取らず False。"""
        with (
            patch.object(proxy_mgr, "is_proxy_running", return_value=False),
            patch.object(proxy_mgr, "_is_port_in_use", return_value=True),
            patch.object(proxy_mgr, "_find_pid_by_port", return_value=12345),
            patch.object(proxy_mgr, "_looks_like_mcp_proxy", return_value=False),
            patch.object(proxy_mgr, "_write_pid") as mock_write,
        ):
            result = proxy_mgr.start_proxy({"command": "test"}, str(tmp_path))
        assert result is False
        mock_write.assert_not_called()
        state = proxy_mgr.read_proxy_state(str(tmp_path))
        assert state["proxy_state"] == "failed"

    def test_launches_supervisor(self, tmp_path):
        """起動時は raw mcp-proxy ではなく supervisor を立ち上げる。"""
        mock_proc = MagicMock()
        mock_proc.pid = 43210

        with (
            patch.object(proxy_mgr, "is_proxy_running", return_value=False),
            patch.object(proxy_mgr, "_is_port_in_use", return_value=False),
            patch.object(proxy_mgr, "cleanup_orphan"),
            patch.object(proxy_mgr, "_wait_for_port", return_value=True),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        ):
            result = proxy_mgr.start_proxy({"command": "test"}, str(tmp_path))

        assert result is True
        launched_call = mock_popen.call_args_list[0]
        launched_cmd = launched_call.args[0]
        launched_env = launched_call.kwargs["env"]
        assert launched_cmd[0] == "python3"
        assert launched_cmd[1].endswith("proxy_supervisor.py")
        assert launched_cmd[2] == str(tmp_path)
        assert launched_env[proxy_mgr._SUPERVISOR_CONFIG_ENV]

        # 起動後 PID ファイルの中身が実 PID になっている
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        assert proxy_mgr._read_pid(pid_path) == 43210

    def test_popen_failure(self, tmp_path):
        """Popen が失敗した場合、False。"""
        with (
            patch.object(proxy_mgr, "is_proxy_running", return_value=False),
            patch.object(proxy_mgr, "_is_port_in_use", return_value=False),
            patch.object(proxy_mgr, "cleanup_orphan"),
            patch("subprocess.Popen", side_effect=OSError("not found")),
        ):
            result = proxy_mgr.start_proxy({"command": "test"}, str(tmp_path))
        assert result is False

    def test_timeout_kills_process(self, tmp_path):
        """タイムアウト時にプロセスを kill して False。"""
        mock_proc = MagicMock()
        mock_proc.pid = 99999

        with (
            patch.object(proxy_mgr, "is_proxy_running", return_value=False),
            patch.object(proxy_mgr, "_is_port_in_use", return_value=False),
            patch.object(proxy_mgr, "cleanup_orphan"),
            patch.object(proxy_mgr, "_wait_for_port", return_value=False),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("os.kill") as mock_kill,
        ):
            result = proxy_mgr.start_proxy(
                {"command": "test", "proxy": {"startup_timeout": 0}},
                str(tmp_path),
            )
        assert result is False
        mock_kill.assert_called()

        # PID ファイルが削除されている
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        assert proxy_mgr._read_pid(pid_path) is None

    def test_port_in_use_restores_pid(self, tmp_path):
        """ポート使用中で早期リターンする際に実プロセスの PID が復元される。"""
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        proxy_mgr._write_pid(pid_path, 99999)  # stale PID

        with (
            patch.object(proxy_mgr, "is_proxy_running", return_value=False),
            patch.object(proxy_mgr, "_is_port_in_use", return_value=True),
            patch.object(proxy_mgr, "_find_pid_by_port", return_value=77777),
            patch.object(proxy_mgr, "_looks_like_mcp_proxy", return_value=True),
        ):
            result = proxy_mgr.start_proxy({"command": "test"}, str(tmp_path))
        assert result is True
        # 実プロセスの PID に書き換えられている
        assert proxy_mgr._read_pid(pid_path) == 77777

    def test_port_in_use_removes_pid_when_lsof_fails(self, tmp_path):
        """lsof で PID を取得できない場合は stale PID ファイルを削除し failed とする。"""
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        proxy_mgr._write_pid(pid_path, 99999)

        with (
            patch.object(proxy_mgr, "is_proxy_running", return_value=False),
            patch.object(proxy_mgr, "_is_port_in_use", return_value=True),
            patch.object(proxy_mgr, "_find_pid_by_port", return_value=None),
        ):
            result = proxy_mgr.start_proxy({"command": "test"}, str(tmp_path))
        assert result is False
        assert proxy_mgr._read_pid(pid_path) is None


class TestStopProxy:
    """stop_proxy のテスト。"""

    def test_no_pid_no_port_process(self, tmp_path):
        """PID ファイルなしでポートプロセスもない場合、True。"""
        with (
            patch.object(proxy_mgr, "_find_pid_by_port", return_value=None),
        ):
            result = proxy_mgr.stop_proxy({}, str(tmp_path))
        assert result is True

    def test_pid_already_dead(self, tmp_path):
        """PID ファイルはあるがプロセスは死亡の場合、クリーンアップして True。"""
        pid_path = tmp_path / ".claude" / ".mcp-proxy.pid"
        pid_path.parent.mkdir(parents=True)
        pid_path.write_text("99999999")

        with patch.object(proxy_mgr, "_is_pid_alive", return_value=False):
            result = proxy_mgr.stop_proxy({}, str(tmp_path))
        assert result is True
        assert not pid_path.exists()

    def test_sigterm_success(self, tmp_path):
        """SIGTERM で正常停止した場合、True。"""
        pid_path = tmp_path / ".claude" / ".mcp-proxy.pid"
        pid_path.parent.mkdir(parents=True)
        pid_path.write_text("12345")

        with (
            patch.object(proxy_mgr, "_is_pid_alive", return_value=True),
            patch("os.kill"),
            patch.object(proxy_mgr, "_wait_for_exit", return_value=True),
        ):
            result = proxy_mgr.stop_proxy({}, str(tmp_path))
        assert result is True
        # PID ファイルが削除されている
        assert proxy_mgr._read_pid(str(pid_path)) is None

    def test_stop_via_port_fallback(self, tmp_path):
        """PID ファイルなしでもポートから検証済み PID を発見して停止できる。"""
        with (
            patch.object(proxy_mgr, "_find_pid_by_port", return_value=77777),
            patch.object(proxy_mgr, "_looks_like_mcp_proxy", return_value=True),
            patch.object(proxy_mgr, "_is_pid_alive", return_value=True),
            patch("os.kill") as mock_kill,
            patch.object(proxy_mgr, "_wait_for_exit", return_value=True),
        ):
            result = proxy_mgr.stop_proxy({}, str(tmp_path))
        assert result is True
        # ポートから発見した PID に SIGTERM が送られている
        import signal

        mock_kill.assert_any_call(77777, signal.SIGTERM)

    def test_stop_via_port_fallback_skips_unverified_pid(self, tmp_path):
        """ポートの PID が mcp-proxy と検証できない場合は kill しない。"""
        with (
            patch.object(proxy_mgr, "_find_pid_by_port", return_value=77777),
            patch.object(proxy_mgr, "_looks_like_mcp_proxy", return_value=False),
            patch("os.kill") as mock_kill,
        ):
            result = proxy_mgr.stop_proxy({}, str(tmp_path))
        assert result is True
        mock_kill.assert_not_called()

    def test_sigkill_fallback(self, tmp_path):
        """SIGTERM で終了しないプロセスに対し SIGKILL へエスカレーションする。"""
        pid_path = tmp_path / ".claude" / ".mcp-proxy.pid"
        pid_path.parent.mkdir(parents=True)
        pid_path.write_text("33333")

        with (
            patch.object(proxy_mgr, "_is_pid_alive", return_value=True),
            patch("os.kill") as mock_kill,
            patch.object(proxy_mgr, "_wait_for_exit", return_value=False),
        ):
            proxy_mgr.stop_proxy({}, str(tmp_path))

        # SIGTERM + SIGKILL が呼ばれている
        import signal

        kill_signals = [call.args[1] for call in mock_kill.call_args_list]
        assert signal.SIGTERM in kill_signals
        assert signal.SIGKILL in kill_signals


class TestStartProxyBackground:
    def test_launches_helper(self, tmp_path):
        with (
            patch.object(proxy_mgr, "get_proxy_state", return_value={"proxy_state": "stopped"}),
            patch("subprocess.Popen") as mock_popen,
        ):
            result = proxy_mgr.start_proxy_background(
                {"command": "test", "proxy": {"port": 8792, "port_range": 0}},
                str(tmp_path),
            )

        assert result is True
        mock_popen.assert_called_once()

        state_path = Path(proxy_mgr.resolve_proxy_state_path(str(tmp_path)))
        state = json.loads(state_path.read_text())
        assert state["proxy_state"] == "starting"

    def test_releases_lock_before_launching_helper(self, tmp_path):
        events: list[str] = []

        def _acquire(_path: str) -> bool:
            events.append("acquire")
            return True

        def _release(_path: str) -> None:
            events.append("release")

        def _popen(*_args, **_kwargs):
            events.append("popen")
            return MagicMock()

        with (
            patch.object(proxy_mgr, "_acquire_lock", side_effect=_acquire),
            patch.object(proxy_mgr, "_release_lock", side_effect=_release),
            patch.object(proxy_mgr, "get_proxy_state", return_value={"proxy_state": "stopped"}),
            patch("subprocess.Popen", side_effect=_popen),
        ):
            result = proxy_mgr.start_proxy_background(
                {"command": "test", "proxy": {"port": 8792, "port_range": 0}},
                str(tmp_path),
            )

        assert result is True
        assert events == ["acquire", "release", "popen"]

    def test_skips_when_ready(self, tmp_path):
        with (
            patch.object(proxy_mgr, "get_proxy_state", return_value={"proxy_state": "ready"}),
            patch("subprocess.Popen") as mock_popen,
        ):
            result = proxy_mgr.start_proxy_background(
                {"command": "test", "proxy": {"port": 8792, "port_range": 0}},
                str(tmp_path),
            )

        assert result is False
        mock_popen.assert_not_called()

    def test_skips_when_starting(self, tmp_path):
        with (
            patch.object(
                proxy_mgr,
                "get_proxy_state",
                return_value={
                    "proxy_state": "starting",
                    "last_transition_at": "2999-01-01T00:00:00+00:00",
                },
            ),
            patch("subprocess.Popen") as mock_popen,
        ):
            result = proxy_mgr.start_proxy_background(
                {"command": "test", "proxy": {"port": 8792, "port_range": 0}},
                str(tmp_path),
            )

        assert result is False
        mock_popen.assert_not_called()

    def test_does_not_block_on_helper_process(self, tmp_path):
        """EV-18: warmup はバックグラウンド起動後、helper の終了を待たずに返る
        （SessionStart hook が同期的にブロックしないことの根拠。Issue #127 仕様確定）。
        """
        helper_process = MagicMock()

        with (
            patch.object(proxy_mgr, "get_proxy_state", return_value={"proxy_state": "stopped"}),
            patch("subprocess.Popen", return_value=helper_process) as mock_popen,
        ):
            result = proxy_mgr.start_proxy_background(
                {"command": "test", "proxy": {"port": 8792, "port_range": 0}},
                str(tmp_path),
            )

        assert result is True
        # start_new_session=True でデタッチし、wait()/communicate() で helper の
        # 終了を待つことはしない（fire-and-forget）。poll() を繰り返して終了を
        # 待つ同期的な実装も許容しない。
        assert mock_popen.call_args.kwargs.get("start_new_session") is True
        helper_process.wait.assert_not_called()
        helper_process.communicate.assert_not_called()
        helper_process.poll.assert_not_called()


class TestFindPidByPort:
    """_find_pid_by_port のテスト。"""

    def test_lsof_success(self):
        """lsof が PID を返す。"""
        import subprocess

        mock_result = subprocess.CompletedProcess(args=["lsof"], returncode=0, stdout="12345\n")
        with patch("subprocess.run", return_value=mock_result):
            result = proxy_mgr._find_pid_by_port(8792)
        assert result == 12345

    def test_lsof_failure(self):
        """lsof 失敗時は None。"""
        import subprocess

        mock_result = subprocess.CompletedProcess(args=["lsof"], returncode=1, stdout="")
        with patch("subprocess.run", return_value=mock_result):
            result = proxy_mgr._find_pid_by_port(8792)
        assert result is None

    def test_lsof_not_found(self):
        """lsof がインストールされていない場合、None。"""
        with patch("subprocess.run", side_effect=OSError("lsof not found")):
            result = proxy_mgr._find_pid_by_port(8792)
        assert result is None

    def test_multiple_pids_returns_first(self):
        """lsof が複数 PID を返した場合、最初の PID を返す。"""
        import subprocess

        mock_result = subprocess.CompletedProcess(
            args=["lsof"], returncode=0, stdout="11111\n22222\n"
        )
        with patch("subprocess.run", return_value=mock_result):
            result = proxy_mgr._find_pid_by_port(8792)
        assert result == 11111

    def test_returns_none_on_timeout(self):
        """subprocess.TimeoutExpired（OSError のサブクラスではない）発生時は None。"""
        import subprocess

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="lsof", timeout=5),
        ):
            result = proxy_mgr._find_pid_by_port(8792)
        assert result is None


class TestLooksLikeMcpProxy:
    """_looks_like_mcp_proxy のテスト。"""

    def test_matches_mcp_proxy_command(self):
        """ps 出力が mcp-proxy コマンドに一致する場合、True。"""
        import subprocess

        mock_result = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout="mcp-proxy --pass-environment --host 127.0.0.1 --port 8792\n",
        )
        with patch("subprocess.run", return_value=mock_result):
            assert proxy_mgr._looks_like_mcp_proxy(12345) is True

    def test_matches_supervisor_command(self):
        """ps 出力が proxy_supervisor.py コマンドに一致する場合、True。"""
        import subprocess

        mock_result = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout="python3 /path/to/proxy_supervisor.py /some/project\n",
        )
        with patch("subprocess.run", return_value=mock_result):
            assert proxy_mgr._looks_like_mcp_proxy(12345) is True

    def test_rejects_unrelated_command(self):
        """無関係なコマンドの場合、False。"""
        import subprocess

        mock_result = subprocess.CompletedProcess(
            args=["ps"], returncode=0, stdout="/usr/bin/some-other-daemon\n"
        )
        with patch("subprocess.run", return_value=mock_result):
            assert proxy_mgr._looks_like_mcp_proxy(12345) is False

    def test_returns_false_on_os_error(self):
        """OSError 発生時は False。"""
        with patch("subprocess.run", side_effect=OSError):
            assert proxy_mgr._looks_like_mcp_proxy(12345) is False

    def test_returns_false_on_timeout(self):
        """subprocess.TimeoutExpired 発生時は False。"""
        import subprocess

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ps", timeout=2),
        ):
            assert proxy_mgr._looks_like_mcp_proxy(12345) is False


class TestCleanupOrphan:
    """cleanup_orphan のテスト。"""

    def test_noop_when_no_pid_file(self, tmp_path):
        """PID ファイルがない場合、例外を出さず何もしない。"""
        proxy_mgr.cleanup_orphan({}, str(tmp_path))

    def test_removes_stale_pid_dead_process(self, tmp_path):
        """プロセスが死亡している場合、stale PID ファイルを削除する。"""
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        proxy_mgr._write_pid(pid_path, 44444)

        with patch.object(proxy_mgr, "_is_pid_alive", return_value=False):
            proxy_mgr.cleanup_orphan({}, str(tmp_path))
        assert proxy_mgr._read_pid(pid_path) is None

    def test_kills_alive_orphan(self, tmp_path):
        """生存中の orphan プロセスは mcp-proxy と検証できれば kill する。"""
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        proxy_mgr._write_pid(pid_path, 55555)

        with (
            patch.object(proxy_mgr, "_is_pid_alive", return_value=True),
            patch.object(proxy_mgr, "_looks_like_mcp_proxy", return_value=True),
            patch("os.kill") as mock_kill,
            patch.object(proxy_mgr, "_wait_for_exit", return_value=True),
        ):
            proxy_mgr.cleanup_orphan({}, str(tmp_path))
        assert proxy_mgr._read_pid(pid_path) is None

        import signal

        mock_kill.assert_any_call(55555, signal.SIGTERM)

    def test_does_not_kill_unverified_pid(self, tmp_path):
        """PID ファイルの PID が mcp-proxy と検証できない場合は kill せず掃除のみ行う。"""
        pid_path = os.path.join(str(tmp_path), ".claude", ".mcp-proxy.pid")
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        proxy_mgr._write_pid(pid_path, 66666)

        with (
            patch.object(proxy_mgr, "_is_pid_alive", return_value=True),
            patch.object(proxy_mgr, "_looks_like_mcp_proxy", return_value=False),
            patch("os.kill") as mock_kill,
        ):
            proxy_mgr.cleanup_orphan({}, str(tmp_path))
        assert proxy_mgr._read_pid(pid_path) is None
        mock_kill.assert_not_called()


class TestBuildProxyCommand:
    """_build_proxy_command のテスト。"""

    def test_builds_command(self):
        """正しいコマンドを組み立てる。"""
        config = {"command": "uvx cocoindex-code", "args": ["--prerelease=explicit"]}
        proxy_cfg = {"host": "127.0.0.1", "port": 8800}
        result = proxy_mgr._build_proxy_command(config, proxy_cfg)
        assert result == [
            "mcp-proxy",
            "--pass-environment",
            "--host",
            "127.0.0.1",
            "--port",
            "8800",
            "--",
            "uvx cocoindex-code",
            "--prerelease=explicit",
        ]

    def test_no_args(self):
        """args 未指定の場合、args なしのコマンドを組み立てる。"""
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

    def test_no_command_raises(self):
        """command がない場合、ValueError。"""
        with pytest.raises(ValueError, match="command"):
            proxy_mgr._build_proxy_command({}, {"port": 8800})
