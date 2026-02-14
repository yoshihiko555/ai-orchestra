#!/usr/bin/env python3
"""SubagentStop hook: サブエージェント終了時に tmux ペインに完了通知を表示する。

SubagentStart hook が作成した tmux ペインに対して、
ペインタイトルとスタイルを更新して完了を視覚的に示す。
tail -f は維持し、出力内容を保持したままにする。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tmux_common import (
    SESSION_INFO_DIR,
    find_claude_pid,
    get_field,
    is_tmux_monitoring_enabled,
    read_hook_input,
    run_tmux,
    tmux_has_session,
)


def read_file(path: str) -> str:
    """ファイルの内容を読み取る。存在しなければ空文字を返す。"""
    try:
        return open(path).read().strip()
    except OSError:
        return ""


def find_pane_by_title(tmux_session: str, agent_id: str) -> str | None:
    """tmux セッション内で agent_id を含むタイトルのペインを探す。

    Returns: ペインID（例: "%5"）。見つからなければ None。
    """
    result = run_tmux(
        "list-panes", "-t", tmux_session, "-F", "#{pane_id}:#{pane_title}"
    )
    if result.returncode != 0:
        return None

    short_id = agent_id[:7]
    for line in result.stdout.strip().splitlines():
        if short_id in line:
            pane_id = line.split(":")[0]
            return pane_id
    return None


def main() -> None:
    data = read_hook_input()
    cwd = get_field(data, "cwd") or os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    if not is_tmux_monitoring_enabled(cwd):
        return

    agent_id = get_field(data, "agent_id")
    agent_type = get_field(data, "agent_type")
    session_id = get_field(data, "session_id")

    if not agent_id or not session_id:
        return

    # セッション情報を読み込む（pane info → tmux-session の順で試行）
    pane_info_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.pane-{agent_id}")
    tmux_session_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.tmux-session")

    tmux_session = read_file(pane_info_file) or read_file(tmux_session_file)

    if not tmux_session:
        # フォールバック: PID ベースのセッション名を試行
        project_name = os.path.basename(cwd) if cwd else "unknown"
        claude_pid = find_claude_pid()
        session_key = str(claude_pid) if claude_pid else session_id[:7]
        tmux_session = f"claude-{project_name}-{session_key}"

    if not tmux_has_session(tmux_session):
        return

    short_id = agent_id[:7]
    label = f"{agent_type}:{short_id}"

    # ペインを特定
    pane_id = find_pane_by_title(tmux_session, agent_id)

    if pane_id:
        # 完了通知: ペインタイトルを更新（視覚的インジケータ）
        run_tmux("select-pane", "-t", pane_id, "-T", f"DONE: {label}")

        # ペインのボーダースタイルを緑に変更
        run_tmux(
            "set-option", "-t", pane_id, "pane-border-style", "fg=green"
        )
        run_tmux(
            "set-option", "-t", pane_id, "pane-active-border-style", "fg=green"
        )

    # pane info ファイルを削除（クリーンアップ）
    try:
        os.remove(pane_info_file)
    except OSError:
        pass


if __name__ == "__main__":
    main()
