"""mcp-proxy のライフサイクル管理モジュール。

mcp-proxy を subprocess として起動・停止し、状態ファイルで追跡する。
標準ライブラリのみ使用（外部依存なし）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import signal
import socket
import subprocess
import time
from typing import Any

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

_STATE_DIR = ".claude/state"
_PROXY_STATE_FILE = "cocoindex-proxy.json"
_SESSIONS_DIR = "cocoindex-sessions"
_START_LOCK_FILE = "cocoindex-proxy.lock"
_PORT_POLL_INTERVAL = 0.3
_EXIT_POLL_INTERVAL = 0.2
_SIGTERM_WAIT = 5
_STATE_STALE_SECONDS = 300


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------


def get_proxy_config(config: dict, project_dir: str = "") -> dict:
    """proxy セクションを取得し、デフォルト値を補完して返す。"""
    proxy = config.get("proxy", {})
    result = {**_DEFAULTS, **proxy}
    if project_dir:
        normalized_dir = _normalize_project_dir(project_dir)
        result["port"] = _derive_port(normalized_dir, result["port"], result["port_range"])
    return result


def build_proxy_url(target: str, config: dict, project_dir: str) -> str:
    """target 向けの proxy URL を返す。"""
    proxy_cfg = get_proxy_config(config, project_dir)
    path = "/mcp" if target == "codex" else "/sse"
    return f"http://{proxy_cfg['host']}:{proxy_cfg['port']}{path}"


def resolve_pid_path(config: dict, project_dir: str) -> str:
    """pid_file を project_dir 相対で解決する。"""
    proxy_cfg = get_proxy_config(config, project_dir)
    pid_file = proxy_cfg["pid_file"]
    if os.path.isabs(pid_file):
        return pid_file
    return os.path.join(project_dir, pid_file)


def resolve_proxy_state_path(project_dir: str) -> str:
    """global proxy state ファイルのパスを返す。"""
    return os.path.join(project_dir, _STATE_DIR, _PROXY_STATE_FILE)


def resolve_session_state_path(project_dir: str, session_id: str) -> str:
    """session state ファイルのパスを返す。"""
    return os.path.join(project_dir, _STATE_DIR, _SESSIONS_DIR, f"{session_id}.json")


def read_proxy_state(project_dir: str) -> dict:
    """global proxy state を読み込む。"""
    return _read_json(resolve_proxy_state_path(project_dir))


def get_proxy_state(config: dict, project_dir: str) -> dict:
    """実ランタイムを反映した proxy state を返す。"""
    proxy_cfg = get_proxy_config(config, project_dir)
    state = _merge_proxy_state(read_proxy_state(project_dir), proxy_cfg)
    port_in_use = _is_port_in_use(proxy_cfg["host"], proxy_cfg["port"])

    if port_in_use:
        pid = _find_pid_by_port(proxy_cfg["port"])
        if pid is not None:
            _write_pid(resolve_pid_path(config, project_dir), pid)
        next_state = {
            **state,
            "proxy_state": "ready",
            "pid": pid or state.get("pid"),
            "last_error": "",
        }
        return _persist_proxy_state_if_changed(project_dir, state, next_state)

    pid_path = resolve_pid_path(config, project_dir)
    pid = _read_pid(pid_path)
    if pid is not None and not _is_pid_alive(pid):
        _remove_pid(pid_path)

    if state.get("proxy_state") == "starting" and not _is_state_stale(state):
        return state

    if state.get("proxy_state") == "starting" and _is_state_stale(state):
        next_state = {
            **state,
            "proxy_state": "failed",
            "pid": None,
            "last_error": state.get("last_error") or "startup timed out",
        }
        return _persist_proxy_state_if_changed(project_dir, state, next_state)

    if state.get("proxy_state") not in {"stopped", "failed"}:
        next_state = {
            **state,
            "proxy_state": "stopped",
            "pid": None,
        }
        return _persist_proxy_state_if_changed(project_dir, state, next_state)

    return state


def update_proxy_state(project_dir: str, config: dict, **updates: Any) -> dict:
    """global proxy state を更新する。"""
    proxy_cfg = get_proxy_config(config, project_dir)
    state = _merge_proxy_state(read_proxy_state(project_dir), proxy_cfg)
    state.update(updates)
    state["host"] = proxy_cfg["host"]
    state["port"] = proxy_cfg["port"]
    state["last_transition_at"] = _utcnow()
    _write_json_atomic(resolve_proxy_state_path(project_dir), state)
    return state


def clear_proxy_state(project_dir: str) -> None:
    """global proxy state を削除する。"""
    _remove_file(resolve_proxy_state_path(project_dir))


def read_session_state(project_dir: str, session_id: str) -> dict:
    """session state を読み込む。"""
    if not session_id:
        return {}
    return _read_json(resolve_session_state_path(project_dir, session_id))


def write_session_state(
    project_dir: str,
    session_id: str,
    *,
    reconnect_required: bool,
    reconnect_notified: bool = False,
) -> dict:
    """session state を書き出す。"""
    if not session_id:
        return {}
    state = {
        "session_id": session_id,
        "reconnect_required": reconnect_required,
        "reconnect_notified": reconnect_notified,
        "updated_at": _utcnow(),
    }
    _write_json_atomic(resolve_session_state_path(project_dir, session_id), state)
    return state


def mark_session_reconnect_notified(project_dir: str, session_id: str) -> dict:
    """reconnect 通知済みフラグを立てる。"""
    state = read_session_state(project_dir, session_id)
    if not state:
        return {}
    state["reconnect_notified"] = True
    state["updated_at"] = _utcnow()
    _write_json_atomic(resolve_session_state_path(project_dir, session_id), state)
    return state


def clear_session_state(project_dir: str, session_id: str) -> None:
    """session state を削除する。"""
    if not session_id:
        return
    _remove_file(resolve_session_state_path(project_dir, session_id))


def start_proxy_background(config: dict, project_dir: str) -> bool:
    """mcp-proxy 起動 helper をバックグラウンドで起動する。"""
    lock_path = _resolve_proxy_lock_path(project_dir)
    if not _acquire_lock(lock_path):
        return False

    try:
        state = get_proxy_state(config, project_dir)
        if state.get("proxy_state") == "ready":
            return False
        if state.get("proxy_state") == "starting" and not _is_state_stale(state):
            return False

        update_proxy_state(
            project_dir,
            config,
            proxy_state="starting",
            pid=None,
            last_error="",
        )

        helper_path = _resolve_background_helper_path()
        env = os.environ.copy()
        env.setdefault("AI_ORCHESTRA_DIR", os.environ.get("AI_ORCHESTRA_DIR", ""))

        try:
            subprocess.Popen(
                ["python3", helper_path, project_dir],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
            return True
        except (OSError, FileNotFoundError, ValueError) as exc:
            update_proxy_state(
                project_dir,
                config,
                proxy_state="failed",
                pid=None,
                last_error=f"launcher failed: {exc}",
            )
            return False
    finally:
        _release_lock(lock_path)


def start_proxy(config: dict, project_dir: str) -> bool:
    """mcp-proxy を同期起動する。helper から呼ばれる。"""
    if is_proxy_running(config, project_dir):
        return True

    lock_path = _resolve_proxy_lock_path(project_dir)
    if not _acquire_lock(lock_path):
        return False

    try:
        if is_proxy_running(config, project_dir):
            return True

        proxy_cfg = get_proxy_config(config, project_dir)
        update_proxy_state(
            project_dir,
            config,
            proxy_state="starting",
            pid=None,
            last_error="",
        )

        # PID ファイルが無効でもポートが使用中なら稼働中とみなす
        if _is_port_in_use(proxy_cfg["host"], proxy_cfg["port"]):
            pid_path = resolve_pid_path(config, project_dir)
            port_pid = _find_pid_by_port(proxy_cfg["port"])
            if port_pid is not None:
                _write_pid(pid_path, port_pid)
            else:
                _remove_pid(pid_path)
            update_proxy_state(
                project_dir,
                config,
                proxy_state="ready",
                pid=port_pid,
                last_error="",
            )
            return True

        cleanup_orphan(config, project_dir)

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
        except (OSError, FileNotFoundError, ValueError) as exc:
            update_proxy_state(
                project_dir,
                config,
                proxy_state="failed",
                pid=None,
                last_error=f"spawn failed: {exc}",
            )
            return False

        _write_pid(pid_path, proc.pid)
        update_proxy_state(
            project_dir,
            config,
            proxy_state="starting",
            pid=proc.pid,
            last_error="",
        )

        if _wait_for_port(proxy_cfg["host"], proxy_cfg["port"], proxy_cfg["startup_timeout"]):
            runtime_pid = _find_pid_by_port(proxy_cfg["port"]) or proc.pid
            _write_pid(pid_path, runtime_pid)
            update_proxy_state(
                project_dir,
                config,
                proxy_state="ready",
                pid=runtime_pid,
                last_error="",
            )
            return True

        try:
            os.kill(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        _remove_pid(pid_path)
        update_proxy_state(
            project_dir,
            config,
            proxy_state="failed",
            pid=None,
            last_error="startup timed out",
        )
        return False
    finally:
        _release_lock(lock_path)


def stop_proxy(config: dict, project_dir: str) -> bool:
    """mcp-proxy を停止する。"""
    pid_path = resolve_pid_path(config, project_dir)
    pid = _read_pid(pid_path)

    if pid is None:
        proxy_cfg = get_proxy_config(config, project_dir)
        port_pid = _find_pid_by_port(proxy_cfg["port"])
        if port_pid is None:
            update_proxy_state(project_dir, config, proxy_state="stopped", pid=None, last_error="")
            return True
        pid = port_pid

    if not _is_pid_alive(pid):
        _remove_pid(pid_path)
        update_proxy_state(project_dir, config, proxy_state="stopped", pid=None, last_error="")
        return True

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        _remove_pid(pid_path)
        update_proxy_state(project_dir, config, proxy_state="stopped", pid=None, last_error="")
        return True

    if _wait_for_exit(pid, _SIGTERM_WAIT):
        _remove_pid(pid_path)
        update_proxy_state(project_dir, config, proxy_state="stopped", pid=None, last_error="")
        return True

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass

    exited = _wait_for_exit(pid, 2)
    _remove_pid(pid_path)
    if exited:
        update_proxy_state(project_dir, config, proxy_state="stopped", pid=None, last_error="")
    return exited


def is_proxy_running(config: dict, project_dir: str) -> bool:
    """proxy が稼働中かチェックする。"""
    state = get_proxy_state(config, project_dir)
    return state.get("proxy_state") == "ready"


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
    """project_dir のハッシュから決定論的にポートを導出する。"""
    if port_range <= 0:
        return base_port
    h = hashlib.md5(project_dir.encode()).hexdigest()
    offset = int(h[:4], 16) % port_range
    return base_port + offset


def _normalize_project_dir(project_dir: str) -> str:
    """ポート導出用に project_dir を正規化する。"""
    return os.path.normcase(os.path.realpath(os.path.abspath(project_dir)))


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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(pid))


def _remove_pid(path: str) -> None:
    """PID ファイルを削除する。"""
    _remove_file(path)


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


def _find_pid_by_port(port: int) -> int | None:
    """lsof でポートを使用しているプロセスの PID を取得する。"""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        first_pid = int(result.stdout.strip().splitlines()[0])
        return first_pid if first_pid > 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


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


def _resolve_state_dir(project_dir: str) -> str:
    return os.path.join(project_dir, _STATE_DIR)


def _resolve_proxy_lock_path(project_dir: str) -> str:
    return os.path.join(_resolve_state_dir(project_dir), _START_LOCK_FILE)


def _resolve_background_helper_path() -> str:
    orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
    if orchestra_dir:
        candidate = os.path.join(orchestra_dir, "packages", "cocoindex", "hooks", "start-mcp-proxy.py")
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "start-mcp-proxy.py")


def _merge_proxy_state(existing: dict, proxy_cfg: dict) -> dict:
    state = {
        "proxy_state": "stopped",
        "pid": None,
        "host": proxy_cfg["host"],
        "port": proxy_cfg["port"],
        "last_transition_at": "",
        "last_error": "",
    }
    state.update(existing)
    state["host"] = proxy_cfg["host"]
    state["port"] = proxy_cfg["port"]
    return state


def _persist_proxy_state_if_changed(project_dir: str, before: dict, after: dict) -> dict:
    if before == after:
        return after
    after = dict(after)
    after["last_transition_at"] = _utcnow()
    _write_json_atomic(resolve_proxy_state_path(project_dir), after)
    return after


def _is_state_stale(state: dict) -> bool:
    updated_at = state.get("last_transition_at") or state.get("updated_at") or ""
    if not updated_at:
        return True
    try:
        if updated_at.endswith("Z"):
            updated_at = updated_at[:-1] + "+00:00"
        ts = dt.datetime.fromisoformat(updated_at)
    except ValueError:
        return True
    return (dt.datetime.now(dt.UTC) - ts).total_seconds() > _STATE_STALE_SECONDS


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_json_atomic(path: str, data: dict) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _acquire_lock(path: str) -> bool:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if _is_lock_stale(path):
            _remove_file(path)
            return _acquire_lock(path)
        return False

    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"pid": os.getpid(), "created_at": _utcnow()}, ensure_ascii=False))
    return True


def _release_lock(path: str) -> None:
    _remove_file(path)


def _is_lock_stale(path: str) -> bool:
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return False
    return age > _STATE_STALE_SECONDS


def _utcnow() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")
