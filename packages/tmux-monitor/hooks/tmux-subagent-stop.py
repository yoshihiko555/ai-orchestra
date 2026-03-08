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


def read_pane_info(path: str) -> tuple[str, str]:
    """pane 情報ファイルを読み込む。(tmux_session, pane_id) を返す。

    新形式: tmux_session\\npane_id
    旧形式: tmux_session のみ（後方互換）
    """
    content = read_file(path)
    if not content:
        return "", ""
    lines = content.splitlines()
    tmux_session = lines[0]
    pane_id = lines[1] if len(lines) >= 2 else ""
    return tmux_session, pane_id


def find_pane_by_title(tmux_session: str, agent_id: str) -> tuple[str, str]:
    """tmux セッション内で agent_id を含むタイトルのペインを探す。

    Returns: (pane_id, pane_title) のタプル。見つからなければ ("", "")。
    """
    result = run_tmux("list-panes", "-t", tmux_session, "-F", "#{pane_id}\t#{pane_title}")
    if result.returncode != 0:
        return "", ""

    short_id = agent_id[:7]
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and short_id in parts[1]:
            return parts[0], parts[1]
    return "", ""


def get_pane_title(pane_id: str) -> str:
    """指定ペインの現在のタイトルを取得する。"""
    result = run_tmux("display-message", "-t", pane_id, "-p", "#{pane_title}")
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


def main() -> None:
    data = read_hook_input()
    cwd = get_field(data, "cwd") or os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    if not is_tmux_monitoring_enabled(cwd):
        return

    agent_id = get_field(data, "agent_id")
    session_id = get_field(data, "session_id")

    if not agent_id or not session_id:
        return

    # セッション情報を読み込む（pane info → tmux-session の順で試行）
    pane_info_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.pane-{agent_id}")
    tmux_session_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.tmux-session")

    tmux_session, pane_id = read_pane_info(pane_info_file)

    if not tmux_session:
        tmux_session = read_file(tmux_session_file)

    if not tmux_session:
        # フォールバック: PID ベースのセッション名を試行
        project_name = os.path.basename(cwd) if cwd else "unknown"
        claude_pid = find_claude_pid()
        session_key = str(claude_pid) if claude_pid else session_id[:7]
        tmux_session = f"claude-{project_name}-{session_key}"

    if not tmux_has_session(tmux_session):
        return

    # ペインを特定（pane_id が保存されていればそれを使い、なければタイトル検索）
    current_title = ""
    if pane_id:
        # pane_id がセッション内に存在するか確認
        check = run_tmux("list-panes", "-t", tmux_session, "-F", "#{pane_id}")
        if check.returncode == 0 and pane_id in check.stdout.splitlines():
            current_title = get_pane_title(pane_id)
        else:
            pane_id = ""
    if not pane_id:
        # フォールバック: タイトルベースの検索（旧形式互換）
        pane_id, current_title = find_pane_by_title(tmux_session, agent_id)

    if pane_id:
        # 完了通知: 現在のタイトル（description 入り）を保持して DONE を付与
        if not current_title.startswith("DONE:"):
            run_tmux("select-pane", "-t", pane_id, "-T", f"DONE: {current_title}")

        # ペインのボーダースタイルを緑に変更
        run_tmux("set-option", "-t", pane_id, "pane-border-style", "fg=green")
        run_tmux("set-option", "-t", pane_id, "pane-active-border-style", "fg=green")

    # pane info ファイルを削除（クリーンアップ）
    try:
        os.remove(pane_info_file)
    except OSError:
        pass


if __name__ == "__main__":
    main()
