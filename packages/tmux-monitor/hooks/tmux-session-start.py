#!/usr/bin/env python3
"""SessionStart hook: Claude Code 起動時に tmux 監視セッションを自動作成する。

セッション名: claude-{project_name}-{claude_pid}
PID ベースにより /resume でも同一セッションを再利用し、重複を防ぐ。
PID 検出失敗時は session_id[:7] にフォールバック (従来動作)。
"""

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tmux_common import (
    SESSION_INFO_DIR,
    SHARED_STORE_PREFIX,
    find_claude_pid,
    get_field,
    is_tmux_monitoring_enabled,
    read_hook_input,
    run_tmux,
    tmux_has_session,
)


def cleanup_orphaned_sessions(project_name: str) -> None:
    """孤児 tmux セッションと対応する session info ファイルを削除する。"""
    prefix = f"claude-{project_name}-"

    # 孤児 tmux セッションの削除
    result = run_tmux("ls", "-F", "#{session_name}")
    if result.returncode == 0:
        for name in result.stdout.strip().splitlines():
            if not name.startswith(prefix):
                continue
            suffix = name[len(prefix) :]
            # suffix が数値 (PID) の場合のみチェック
            if suffix.isdigit():
                try:
                    os.kill(int(suffix), 0)
                except OSError:
                    # プロセスが存在しない → 孤児セッション
                    run_tmux("kill-session", "-t", name)

    # 孤児 session info ファイルの削除
    if not os.path.isdir(SESSION_INFO_DIR):
        return
    for filename in os.listdir(SESSION_INFO_DIR):
        if not filename.endswith(".pid"):
            continue
        pid_path = os.path.join(SESSION_INFO_DIR, filename)
        try:
            stored_pid = open(pid_path).read().strip()
        except OSError:
            continue
        if not stored_pid.isdigit():
            continue
        try:
            os.kill(int(stored_pid), 0)
        except OSError:
            # PID が死んでいる → 関連ファイルを削除
            sid = filename[: -len(".pid")]
            for ext in (".tmux-session", ".lock-path", ".pid"):
                try:
                    os.remove(os.path.join(SESSION_INFO_DIR, sid + ext))
                except OSError:
                    pass


def main() -> None:
    data = read_hook_input()
    cwd = get_field(data, "cwd")

    if not cwd or not is_tmux_monitoring_enabled(cwd):
        return

    session_id = get_field(data, "session_id")
    if not session_id:
        return

    project_name = os.path.basename(cwd)
    os.makedirs(SESSION_INFO_DIR, exist_ok=True)

    # PID ベースのセッション名を構築 (フォールバック: session_id[:7])
    claude_pid = find_claude_pid()
    session_key = str(claude_pid) if claude_pid else session_id[:7]

    tmux_session = f"claude-{project_name}-{session_key}"
    first_agent_lock = f"/tmp/claude-subagent-first-{session_key}"

    cleanup_orphaned_sessions(project_name)

    # セッション情報を保存 (SubagentStart / SessionEnd から参照)
    for suffix, content in [
        (".tmux-session", tmux_session),
        (".lock-path", first_agent_lock),
        (".pid", session_key),
    ]:
        with open(os.path.join(SESSION_INFO_DIR, session_id + suffix), "w") as f:
            f.write(content)

    # 前回のロックをクリーンアップ
    try:
        os.rmdir(first_agent_lock)
    except OSError:
        pass

    # tmux セッションの作成/再利用
    if tmux_has_session(tmux_session):
        # /clear や /resume 時: セッションを維持し、古いペインだけ掃除する
        # （kill-session すると attach 中のクライアントが切断されるため）
        result = run_tmux("list-panes", "-t", tmux_session, "-F", "#{pane_id}")
        if result.returncode == 0:
            pane_ids = [p for p in result.stdout.strip().splitlines() if p]
            if pane_ids:
                # 最初のペインを待機画面で respawn
                wait_cmd = f"echo 'Waiting for sub agents...' && echo '({project_name} / PID:{session_key})' && echo '($(date))' && cat"
                run_tmux("respawn-pane", "-t", pane_ids[0], "-k", wait_cmd)
                # 残りのペインを削除
                for pane_id in pane_ids[1:]:
                    run_tmux("kill-pane", "-t", pane_id)
    else:
        run_tmux(
            "new-session",
            "-d",
            "-s",
            tmux_session,
            f"echo 'Waiting for sub agents...' && echo '({project_name} / PID:{session_key})' && echo '($(date))' && cat",
        )

    # 共有コンテキストストアの作成
    shared_dir = f"{SHARED_STORE_PREFIX}{session_key}"
    entries_dir = os.path.join(shared_dir, "entries")
    os.makedirs(entries_dir, exist_ok=True)

    meta = {
        "session_key": session_key,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "project": project_name,
    }
    with open(os.path.join(shared_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    with open(os.path.join(SESSION_INFO_DIR, session_id + ".shared-dir"), "w") as f:
        f.write(shared_dir)


if __name__ == "__main__":
    main()
