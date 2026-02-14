#!/usr/bin/env python3
"""tmux サブエージェント監視フック間で共有する共通関数。"""

import json
import os
import subprocess

from hook_common import get_field, read_hook_input  # noqa: F401 (re-export)

SESSION_INFO_DIR = "/tmp/claude-session-info"
SHARED_STORE_PREFIX = "/tmp/claude-shared-"


def find_claude_pid() -> int | None:
    """プロセスツリーを遡って claude プロセスの PID を探す。

    成功時: PID (int) を返す
    失敗時: None を返す
    """
    pid = os.getppid()
    max_depth = 5

    for _ in range(max_depth):
        if pid <= 1:
            return None

        try:
            result = subprocess.run(
                ["ps", "-o", "comm=", "-p", str(pid)],
                capture_output=True,
                text=True,
            )
            comm = result.stdout.strip()
        except OSError:
            return None

        if not comm:
            return None

        if "claude" in comm:
            return pid

        try:
            result = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True,
                text=True,
            )
            pid = int(result.stdout.strip())
        except (OSError, ValueError):
            return None

    return None


def run_tmux(*args: str) -> subprocess.CompletedProcess[str]:
    """tmux コマンドを実行する。エラーは無視する。"""
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
    )


def tmux_has_session(session_name: str) -> bool:
    """tmux セッションが存在するか確認する。"""
    result = run_tmux("has-session", "-t", session_name)
    return result.returncode == 0


def is_tmux_monitoring_enabled(cwd: str) -> bool:
    """tmux 監視が有効かどうかを判定する。

    以下の条件をすべて満たす場合に True:
    1. tmux がインストールされている
    2. orchestration-flags.yaml で tmux_monitoring.enabled が true
    """
    import shutil

    if not shutil.which("tmux"):
        return False

    flags_path = os.path.join(cwd, ".claude", "config", "orchestration-flags.yaml")
    try:
        with open(flags_path) as f:
            flags = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return False

    feature = (flags.get("features") or {}).get("tmux_monitoring") or {}
    return bool(feature.get("enabled", False))
