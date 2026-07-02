#!/usr/bin/env python3
"""tmux サブエージェント監視フック間で共有する共通関数。

packages 版: orchestration-flags.yaml ではなく tmux の存在チェックのみで有効判定。
パッケージをアンインストールすれば無効になる。
"""

import os
import shutil
import subprocess
import sys

# core パッケージの hook_common を参照
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

from hook_common import get_field, read_hook_input  # noqa: F401 (re-export)

SESSION_INFO_DIR = "/tmp/claude-session-info"
SESSION_FILE_EXTENSIONS = (
    ".tmux-session",
    ".lock-path",
    ".pid",
    ".shared-dir",
    ".task-queue",
)
SHARED_STORE_PREFIX = "/tmp/claude-shared-"
_SUBPROCESS_TIMEOUT = 5.0


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
                timeout=_SUBPROCESS_TIMEOUT,
            )
            comm = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
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
                timeout=_SUBPROCESS_TIMEOUT,
            )
            pid = int(result.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    return None


def run_tmux(*args: str, timeout: float = _SUBPROCESS_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """tmux コマンドを実行する。エラーは無視する。"""
    command = ["tmux", *args]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=command,
            returncode=1,
            stdout="",
            stderr="timeout",
        )


def shell_quote(s: str) -> str:
    """シェル安全なシングルクォートエスケープ。"""
    return "'" + s.replace("'", "'\\''") + "'"


def remove_session_files(session_id: str) -> None:
    """セッション情報ファイルと共有ストアを削除する。"""
    shared_dir_file = os.path.join(SESSION_INFO_DIR, session_id + ".shared-dir")
    try:
        shared_dir = open(shared_dir_file).read().strip()
    except OSError:
        shared_dir = ""

    if shared_dir and os.path.isdir(shared_dir):
        shutil.rmtree(shared_dir, ignore_errors=True)

    for extension in SESSION_FILE_EXTENSIONS:
        try:
            os.remove(os.path.join(SESSION_INFO_DIR, session_id + extension))
        except OSError:
            pass


def tmux_has_session(session_name: str) -> bool:
    """tmux セッションが存在するか確認する。"""
    result = run_tmux("has-session", "-t", session_name)
    return result.returncode == 0


def is_tmux_monitoring_enabled(cwd: str) -> bool:
    """tmux 監視が有効かどうかを判定する。

    プラグイン版: tmux がインストールされていれば有効。
    プラグインをアンインストールすれば無効になる。
    """
    return bool(shutil.which("tmux"))
