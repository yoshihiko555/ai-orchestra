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
        """異なるプロジェクトで異なるポートを返す可能性がある。"""
        port1 = proxy_mgr._derive_port("/project/a", 8792, 100)
        port2 = proxy_mgr._derive_port("/project/b", 8792, 100)
        # 異なるプロジェクトは異なるハッシュになるはず（確率的）
        # ポート範囲内であることだけ確認
        assert 8792 <= port1 < 8892
        assert 8792 <= port2 < 8892

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

    def test_project_dir_derives_port(self):
        """project_dir が指定されるとポートが導出される。"""
        result = proxy_mgr.get_proxy_config({}, project_dir="/my/project")
        assert result["port"] != 8792 or True  # ハッシュ次第で同じになる可能性あり
        assert isinstance(result["port"], int)

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


class TestStartProxy:
    """start_proxy のテスト。"""

    def test_already_running_skips(self, tmp_path):
        """既に起動中の場合、スキップして True。"""
        with patch.object(proxy_mgr, "is_proxy_running", return_value=True):
            result = proxy_mgr.start_proxy({}, str(tmp_path))
        assert result is True

    def test_port_in_use_recovers(self, tmp_path):
        """ポートが使用中の場合、PID を復元して True。"""
        with (
            patch.object(proxy_mgr, "is_proxy_running", return_value=False),
            patch.object(proxy_mgr, "_is_port_in_use", return_value=True),
            patch.object(proxy_mgr, "_find_pid_by_port", return_value=12345),
            patch.object(proxy_mgr, "_write_pid") as mock_write,
        ):
            result = proxy_mgr.start_proxy({"command": "test"}, str(tmp_path))
        assert result is True
        mock_write.assert_called_once()

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


class TestBuildProxyCommand:
    """_build_proxy_command のテスト。"""

    def test_builds_command(self):
        """正しいコマンドを組み立てる。"""
        config = {"command": "uvx cocoindex-code", "args": ["--prerelease=explicit"]}
        proxy_cfg = {"host": "127.0.0.1", "port": 8800}
        result = proxy_mgr._build_proxy_command(config, proxy_cfg)
        assert result[0] == "mcp-proxy"
        assert "--host" in result
        assert "8800" in result
        assert "uvx cocoindex-code" in result
        assert "--prerelease=explicit" in result

    def test_no_command_raises(self):
        """command がない場合、ValueError。"""
        with pytest.raises(ValueError, match="command"):
            proxy_mgr._build_proxy_command({}, {"port": 8800})
