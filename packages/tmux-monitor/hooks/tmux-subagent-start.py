#!/usr/bin/env python3
"""SubagentStart hook: tmux にペインを追加して sub agent の出力をリアルタイム監視する。

SessionStart hook が保存したセッション情報を参照して
正しい tmux セッションにペインを追加する。
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
    shell_quote,
    tmux_has_session,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FORMATTER = os.path.join(SCRIPT_DIR, "tmux-format-output.py")


def read_file(path: str) -> str:
    """ファイルの内容を読み取る。存在しなければ空文字を返す。"""
    try:
        return open(path).read().strip()
    except OSError:
        return ""


def pop_task_description(session_id: str) -> str:
    """PreToolUse hook が保存した description をキューから取得する（FIFO）。"""
    import fcntl
    import json

    queue_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.task-queue")
    try:
        with open(queue_file, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            lines = f.readlines()
            description = ""
            if lines:
                entry = json.loads(lines[0])
                description = entry.get("description", "")
                f.seek(0)
                f.writelines(lines[1:])
                f.truncate()
            fcntl.flock(f, fcntl.LOCK_UN)
            return description
    except (OSError, json.JSONDecodeError, ValueError):
        return ""


def get_current_pane_id(tmux_session: str) -> str:
    """セッションの現在アクティブなペイン ID を取得する。"""
    result = run_tmux("display-message", "-t", tmux_session, "-p", "#{pane_id}")
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return ""


def resolve_tmux_session(cwd: str, session_id: str) -> tuple[str, str]:
    """保存済み情報またはフォールバックから tmux セッションを解決する。"""
    tmux_session_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.tmux-session")
    lock_path_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.lock-path")

    tmux_session = read_file(tmux_session_file)
    first_agent_lock = read_file(lock_path_file)

    if tmux_session and first_agent_lock:
        return tmux_session, first_agent_lock

    project_name = os.path.basename(cwd)
    claude_pid = find_claude_pid()
    session_key = str(claude_pid) if claude_pid else session_id[:7]
    return (
        f"claude-{project_name}-{session_key}",
        f"/tmp/claude-subagent-first-{session_key}",
    )


def snapshot_panes(tmux_session: str) -> tuple[list[str], str]:
    """現在の DONE ペインと最初の待機ペインを一度だけ取得する。"""
    if not tmux_has_session(tmux_session):
        return [], ""

    result = run_tmux("list-panes", "-t", tmux_session, "-F", "#{pane_id}\t#{pane_title}")
    done_panes: list[str] = []
    waiting_pane_id = ""
    if result.returncode == 0:
        lines = [line for line in result.stdout.strip().splitlines() if line]
        for line in lines:
            parts = line.split("\t", 1)
            if len(parts) == 2:
                if parts[1].startswith("DONE:"):
                    done_panes.append(parts[0])
                elif not waiting_pane_id:
                    waiting_pane_id = parts[0]
    return done_panes, waiting_pane_id


def reuse_done_pane(session_id: str, tail_cmd: str, pane_title: str, done_panes: list[str]) -> str:
    """DONE ペインを排他的に予約し、再利用できたペイン ID を返す。"""
    for done_pane in done_panes:
        claim_path = os.path.join(SESSION_INFO_DIR, f"{session_id}.claim-{done_pane}")
        try:
            os.mkdir(claim_path)
        except OSError:
            continue

        resp = run_tmux("respawn-pane", "-t", done_pane, "-k", tail_cmd)
        if resp.returncode == 0:
            run_tmux("select-pane", "-t", done_pane, "-T", pane_title)
            try:
                os.rmdir(claim_path)
            except OSError:
                pass
            return done_pane

        try:
            os.rmdir(claim_path)
        except OSError:
            pass
    return ""


def create_agent_pane(
    tmux_session: str,
    first_agent_lock: str,
    waiting_pane_id: str,
    tail_cmd: str,
) -> str:
    """待機ペインの置換、分割、または新規セッション作成でペインを用意する。"""
    pane_id = ""
    need_split = False
    if tmux_has_session(tmux_session):
        try:
            os.mkdir(first_agent_lock)
            target_pane = waiting_pane_id or get_current_pane_id(tmux_session)
            resp = run_tmux("respawn-pane", "-t", target_pane, "-k", tail_cmd)
            if resp.returncode == 0:
                pane_id = target_pane
            else:
                need_split = True
        except OSError:
            need_split = True
        if need_split:
            MAX_SPLIT_RETRIES = 3
            for _attempt in range(MAX_SPLIT_RETRIES):
                run_tmux("select-layout", "-t", tmux_session, "tiled")
                result = run_tmux(
                    "split-window", "-t", tmux_session, "-P", "-F", "#{pane_id}", tail_cmd
                )
                if result.returncode == 0 and result.stdout.strip():
                    pane_id = result.stdout.strip()
                    break
            run_tmux("select-layout", "-t", tmux_session, "tiled")
    else:
        run_tmux("new-session", "-d", "-s", tmux_session, tail_cmd)
        pane_id = get_current_pane_id(tmux_session)
    return pane_id


def persist_pane_info(session_id: str, agent_id: str, tmux_session: str, pane_id: str) -> None:
    """エージェントと tmux ペインの対応情報を保存する。"""
    pane_info_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.pane-{agent_id}")
    try:
        os.makedirs(SESSION_INFO_DIR, exist_ok=True)
        with open(pane_info_file, "w") as f:
            f.write(f"{tmux_session}\n{pane_id}")
    except OSError:
        pass


def main() -> None:
    data = read_hook_input()
    cwd = get_field(data, "cwd")

    if not cwd or not is_tmux_monitoring_enabled(cwd):
        return

    agent_id = get_field(data, "agent_id")
    agent_type = get_field(data, "agent_type")
    session_id = get_field(data, "session_id")
    transcript_path = get_field(data, "transcript_path")

    if not agent_id or not transcript_path or not session_id:
        return

    tmux_session, first_agent_lock = resolve_tmux_session(cwd, session_id)

    # sub agent の出力ファイルパスを構築
    session_dir = transcript_path.removesuffix(".jsonl")
    output_file = f"{session_dir}/subagents/agent-{agent_id}.jsonl"

    # PreToolUse hook が保存した description を取得
    description = pop_task_description(session_id)
    if description:
        pane_title = f"{description} ({agent_type}:{agent_id[:7]})"
    else:
        pane_title = f"{agent_type}:{agent_id[:7]}"

    # ファイル待機 + tail コマンドを構築 (tmux ペイン内で実行される)
    # シェルインジェクション防止: 外部由来の値をエスケープ
    safe_title = shell_quote(f"=== {pane_title} ===")
    safe_output = shell_quote(output_file)
    wait_and_tail = f"echo {safe_title} && while [ ! -f {safe_output} ]; do sleep 0.3; done && tail -f {safe_output}"

    if os.path.isfile(FORMATTER) and os.access(FORMATTER, os.X_OK):
        tail_cmd = f"{wait_and_tail} | {shell_quote(FORMATTER)}"
    else:
        tail_cmd = wait_and_tail

    done_panes, waiting_pane_id = snapshot_panes(tmux_session)
    pane_id = reuse_done_pane(session_id, tail_cmd, pane_title, done_panes)
    if not pane_id:
        pane_id = create_agent_pane(tmux_session, first_agent_lock, waiting_pane_id, tail_cmd)

    # ペインタイトルを設定（明示的なペイン ID 指定で競合回避）
    if pane_id:
        run_tmux("select-pane", "-t", pane_id, "-T", pane_title)
    else:
        # フォールバック: pane_id が取れなかった場合はセッション指定
        run_tmux("select-pane", "-t", tmux_session, "-T", pane_title)

    persist_pane_info(session_id, agent_id, tmux_session, pane_id)


if __name__ == "__main__":
    main()
