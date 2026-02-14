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

    # SessionStart が保存したセッション情報を読み込む
    tmux_session_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.tmux-session")
    lock_path_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.lock-path")

    tmux_session = read_file(tmux_session_file)
    first_agent_lock = read_file(lock_path_file)

    if not tmux_session or not first_agent_lock:
        # フォールバック: SessionStart が動いていない場合
        project_name = os.path.basename(cwd)

        claude_pid = find_claude_pid()
        session_key = str(claude_pid) if claude_pid else session_id[:7]

        tmux_session = f"claude-{project_name}-{session_key}"
        first_agent_lock = f"/tmp/claude-subagent-first-{session_key}"

    # sub agent の出力ファイルパスを構築
    session_dir = transcript_path.removesuffix(".jsonl")
    output_file = f"{session_dir}/subagents/agent-{agent_id}.jsonl"

    pane_title = f"{agent_type}:{agent_id[:7]}"

    # ファイル待機 + tail コマンドを構築 (tmux ペイン内で実行される)
    wait_and_tail = f"echo '=== {pane_title} ===' && while [ ! -f '{output_file}' ]; do sleep 0.3; done && tail -f '{output_file}'"

    if os.path.isfile(FORMATTER) and os.access(FORMATTER, os.X_OK):
        tail_cmd = f"{wait_and_tail} | '{FORMATTER}'"
    else:
        tail_cmd = wait_and_tail

    # DONE ペインのクリーンアップ + 再利用
    respawned = False
    if tmux_has_session(tmux_session):
        result = run_tmux(
            "list-panes", "-t", tmux_session, "-F", "#{pane_id}\t#{pane_title}"
        )
        if result.returncode == 0:
            lines = [l for l in result.stdout.strip().splitlines() if l]
            pane_count = len(lines)
            done_panes: list[str] = []
            for line in lines:
                parts = line.split("\t", 1)
                if len(parts) == 2 and parts[1].startswith("DONE:"):
                    done_panes.append(parts[0])

            if done_panes:
                # 最初の DONE ペインを新エージェントで respawn（再利用）
                run_tmux("respawn-pane", "-t", done_panes[0], "-k", tail_cmd)
                respawned = True
                # 残りの DONE ペインを kill（ペインが1つにならないようガード）
                for dp in done_panes[1:]:
                    if pane_count <= 1:
                        break
                    run_tmux("kill-pane", "-t", dp)
                    pane_count -= 1

    # tmux セッションにペインを追加（DONE ペインを再利用しなかった場合）
    if respawned:
        pass  # respawn 済み
    elif tmux_has_session(tmux_session):
        # mkdir はアトミック操作 - 最初の1つだけが成功する
        try:
            os.mkdir(first_agent_lock)
            # 最初の sub agent: 待機ペインを置き換え
            run_tmux("respawn-pane", "-t", tmux_session, "-k", tail_cmd)
        except OSError:
            # 2つ目以降: ペインを追加
            run_tmux("split-window", "-t", tmux_session, tail_cmd)
            run_tmux("select-layout", "-t", tmux_session, "tiled")
    else:
        # SessionStart hook が動いていない場合のフォールバック
        run_tmux("new-session", "-d", "-s", tmux_session, tail_cmd)

    # ペインタイトルを設定（SubagentStop でのペイン特定用）
    run_tmux("select-pane", "-t", tmux_session, "-T", pane_title)

    # agent_id -> pane 情報を保存
    pane_info_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.pane-{agent_id}")
    try:
        os.makedirs(SESSION_INFO_DIR, exist_ok=True)
        with open(pane_info_file, "w") as f:
            f.write(tmux_session)
    except OSError:
        pass


if __name__ == "__main__":
    main()
