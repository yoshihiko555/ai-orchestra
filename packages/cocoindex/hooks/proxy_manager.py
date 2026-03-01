"""mcp-proxy のライフサイクル管理モジュール。

mcp-proxy を subprocess として起動・停止し、PID ファイルで状態を追跡する。
標準ライブラリのみ使用（外部依存なし）。
"""

from __future__ import annotations

import hashlib
import os
import signal
import socket
import subprocess
import time

# ---------------------------------------------------------------------------
# デフォルト設定
# ---------------------------------------------------------------------------

_DEFAULTS: dict = {
    "enabled": False,
    "port": 8792,
    "port_range": 100,
    "host": "127.0.0.1",
    "pid_file": ".claude/.mcp-proxy.pid",
    "startup_timeout": 10,
}

_PORT_POLL_INTERVAL = 0.3
_EXIT_POLL_INTERVAL = 0.2
_SIGTERM_WAIT = 5


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------


def get_proxy_config(config: dict, project_dir: str = "") -> dict:
    """proxy セクションを取得し、デフォルト値を補完して返す。

    project_dir が指定された場合、port をプロジェクト固有値に自動導出する。
    port_range が 0 以下の場合は port をそのまま使う（固定モード）。
    """
    proxy = config.get("proxy", {})
    result = {**_DEFAULTS, **proxy}
    if project_dir:
        result["port"] = _derive_port(project_dir, result["port"], result["port_range"])
    return result


def resolve_pid_path(config: dict, project_dir: str) -> str:
    """pid_file を project_dir 相対で解決する。"""
    proxy_cfg = get_proxy_config(config, project_dir)
    pid_file = proxy_cfg["pid_file"]
    if os.path.isabs(pid_file):
        return pid_file
    return os.path.join(project_dir, pid_file)


def start_proxy(config: dict, project_dir: str) -> bool:
    """mcp-proxy を起動する。既に起動中ならスキップ（冪等）。

    Returns:
        True: proxy が利用可能になった
        False: 起動失敗
    """
    if is_proxy_running(config, project_dir):
        return True

    cleanup_orphan(config, project_dir)

    proxy_cfg = get_proxy_config(config, project_dir)
    cmd = _build_proxy_command(config, proxy_cfg)
    pid_path = resolve_pid_path(config, project_dir)

    pid_dir = os.path.dirname(pid_path)
    if pid_dir:
        os.makedirs(pid_dir, exist_ok=True)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, FileNotFoundError, ValueError):
        return False

    _write_pid(pid_path, proc.pid)

    host = proxy_cfg["host"]
    port = proxy_cfg["port"]
    timeout = proxy_cfg["startup_timeout"]

    if _wait_for_port(host, port, timeout):
        return True

    # タイムアウト — プロセスを kill して失敗
    try:
        os.kill(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    _remove_pid(pid_path)
    return False


def stop_proxy(config: dict, project_dir: str) -> bool:
    """mcp-proxy を停止する。

    Returns:
        True: 停止成功（または既に停止済み）
        False: 停止失敗
    """
    pid_path = resolve_pid_path(config, project_dir)
    pid = _read_pid(pid_path)

    if pid is None:
        return True

    if not _is_pid_alive(pid):
        _remove_pid(pid_path)
        return True

    # SIGTERM → 待機 → SIGKILL
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        _remove_pid(pid_path)
        return True

    if _wait_for_exit(pid, _SIGTERM_WAIT):
        _remove_pid(pid_path)
        return True

    # SIGKILL フォールバック
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass

    exited = _wait_for_exit(pid, 2)
    _remove_pid(pid_path)
    return exited


def is_proxy_running(config: dict, project_dir: str) -> bool:
    """proxy が稼働中かチェックする（PID 生死 + ポート使用中）。"""
    pid_path = resolve_pid_path(config, project_dir)
    pid = _read_pid(pid_path)

    if pid is None:
        return False

    if not _is_pid_alive(pid):
        return False

    proxy_cfg = get_proxy_config(config, project_dir)
    return _is_port_in_use(proxy_cfg["host"], proxy_cfg["port"])


def cleanup_orphan(config: dict, project_dir: str) -> None:
    """stale PID ファイルを検出し、残存プロセスをクリーンアップする。"""
    pid_path = resolve_pid_path(config, project_dir)
    pid = _read_pid(pid_path)

    if pid is None:
        return

    if _is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        _wait_for_exit(pid, 3)
        if _is_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    _remove_pid(pid_path)


# ---------------------------------------------------------------------------
# 内部ヘルパー
# ---------------------------------------------------------------------------


def _derive_port(project_dir: str, base_port: int, port_range: int) -> int:
    """project_dir のハッシュから決定論的にポートを導出する。

    port_range が 0 以下の場合は base_port をそのまま返す（固定モード）。
    """
    if port_range <= 0:
        return base_port
    h = hashlib.md5(project_dir.encode()).hexdigest()
    offset = int(h[:4], 16) % port_range
    return base_port + offset


def _read_pid(path: str) -> int | None:
    """PID ファイルから PID を読み込む。"""
    try:
        with open(path, encoding="utf-8") as f:
            pid = int(f.read().strip())
        return pid if pid > 0 else None
    except (OSError, ValueError):
        return None


def _write_pid(path: str, pid: int) -> None:
    """PID ファイルに書き出す。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(pid))


def _remove_pid(path: str) -> None:
    """PID ファイルを削除する。"""
    try:
        os.remove(path)
    except OSError:
        pass


def _is_pid_alive(pid: int) -> bool:
    """PID が生存しているかチェックする。"""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_port_in_use(host: str, port: int) -> bool:
    """指定ホスト:ポートが使用中かチェックする。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def _wait_for_port(host: str, port: int, timeout: float) -> bool:
    """ポートが開くまで待機する。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_port_in_use(host, port):
            return True
        time.sleep(_PORT_POLL_INTERVAL)
    return False


def _wait_for_exit(pid: int, timeout: float) -> bool:
    """プロセスが終了するまで待機する。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_pid_alive(pid):
            return True
        time.sleep(_EXIT_POLL_INTERVAL)
    return False


def _build_proxy_command(config: dict, proxy_cfg: dict) -> list[str]:
    """mcp-proxy 起動コマンドを組み立てる。"""
    command = config.get("command")
    if not command:
        raise ValueError("config['command'] is required to build proxy command")
    cmd = [
        "mcp-proxy",
        "--pass-environment",
        "--host",
        str(proxy_cfg.get("host", "127.0.0.1")),
        "--port",
        str(proxy_cfg["port"]),
        "--",
        command,
    ]
    cmd.extend(config.get("args", []))
    return cmd
