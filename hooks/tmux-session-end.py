#!/usr/bin/env python3
"""SessionEnd hook: Claude Code 終了時に tmux 監視セッションを自動削除する。

現在の session_id だけでなく、同一 PID に紐づく全 session info を一括クリーンアップする。
(/resume で session_id が変わっても旧ファイルが残らない)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tmux_common import (
    SESSION_INFO_DIR,
    get_field,
    is_tmux_monitoring_enabled,
    read_hook_input,
    run_tmux,
)


def read_file(path: str) -> str:
    """ファイルの内容を読み取る。存在しなければ空文字を返す。"""
    try:
        return open(path).read().strip()
    except OSError:
        return ""


def remove_silent(path: str) -> None:
    """ファイルを削除する。存在しなくてもエラーにしない。"""
    try:
        os.remove(path)
    except OSError:
        pass


def rmdir_silent(path: str) -> None:
    """ディレクトリを削除する。存在しなくてもエラーにしない。"""
    try:
        os.rmdir(path)
    except OSError:
        pass


def main() -> None:
    data = read_hook_input()
    cwd = get_field(data, "cwd") or os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    if not is_tmux_monitoring_enabled(cwd):
        return

    session_id = get_field(data, "session_id")
    if not session_id:
        return

    tmux_session_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.tmux-session")
    lock_path_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.lock-path")
    pid_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.pid")

    # 現在の session_id から tmux セッションを削除
    tmux_session = read_file(tmux_session_file)
    if tmux_session:
        run_tmux("kill-session", "-t", tmux_session)

    lock_path = read_file(lock_path_file)
    if lock_path:
        rmdir_silent(lock_path)

    # 共有コンテキストストアの削除
    shared_dir_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.shared-dir")
    shared_dir = read_file(shared_dir_file)
    if shared_dir and os.path.isdir(shared_dir):
        import shutil

        shutil.rmtree(shared_dir, ignore_errors=True)
    remove_silent(shared_dir_file)

    # 同一 PID に紐づく全 session info ファイルを一括クリーンアップ
    current_pid = read_file(pid_file)

    if current_pid and os.path.isdir(SESSION_INFO_DIR):
        for filename in os.listdir(SESSION_INFO_DIR):
            if not filename.endswith(".pid"):
                continue
            filepath = os.path.join(SESSION_INFO_DIR, filename)
            stored_pid = read_file(filepath)
            if stored_pid != current_pid:
                continue

            sid = filename[: -len(".pid")]

            # この session_id に紐づくロックもクリーンアップ
            lock = read_file(os.path.join(SESSION_INFO_DIR, f"{sid}.lock-path"))
            if lock:
                rmdir_silent(lock)

            # 関連する共有ストアもクリーンアップ
            sd = read_file(os.path.join(SESSION_INFO_DIR, f"{sid}.shared-dir"))
            if sd and os.path.isdir(sd):
                import shutil

                shutil.rmtree(sd, ignore_errors=True)

            for ext in (".tmux-session", ".lock-path", ".pid", ".shared-dir"):
                remove_silent(os.path.join(SESSION_INFO_DIR, sid + ext))
    else:
        # PID ファイルが無い場合 (旧形式): 現在の session_id のみクリーンアップ
        remove_silent(tmux_session_file)
        remove_silent(lock_path_file)


if __name__ == "__main__":
    main()
