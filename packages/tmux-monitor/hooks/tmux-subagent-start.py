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


def shell_quote(s: str) -> str:
    """シェル安全なシングルクォートエスケープ。"""
    return "'" + s.replace("'", "'\\''") + "'"


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

    # ペイン ID を追跡（並列起動時のレースコンディション回避）
    pane_id = ""

    # 現在のペイン一覧をスナップショットとして取得（並列 split-window の前に確定）
    # waiting_pane_id: 最初のエージェントが respawn する対象
    waiting_pane_id = ""
    done_panes: list[str] = []

    # DONE ペインの再利用（並列安全: 各エージェントが1つだけ予約）
    respawned = False
    if tmux_has_session(tmux_session):
        result = run_tmux("list-panes", "-t", tmux_session, "-F", "#{pane_id}\t#{pane_title}")
        if result.returncode == 0:
            lines = [l for l in result.stdout.strip().splitlines() if l]
            for line in lines:
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    if parts[1].startswith("DONE:"):
                        done_panes.append(parts[0])
                    elif not waiting_pane_id:
                        # DONE でない最初のペイン = 待機ペイン候補
                        waiting_pane_id = parts[0]

            # DONE ペインを1つだけ予約して respawn（mkdir でアトミックに排他制御）
            for dp in done_panes:
                claim_path = os.path.join(SESSION_INFO_DIR, f"{session_id}.claim-{dp}")
                try:
                    os.mkdir(claim_path)
                except OSError:
                    # 他のプロセスが先に予約した → 次の DONE ペインを試す
                    continue
                # このプロセスが dp を予約できた
                resp = run_tmux("respawn-pane", "-t", dp, "-k", tail_cmd)
                # claim を即座に解放（排他制御は mkdir の瞬間のみ必要）
                try:
                    os.rmdir(claim_path)
                except OSError:
                    pass
                if resp.returncode == 0:
                    pane_id = dp
                    respawned = True
                    break
                # respawn 失敗（ペイン消滅等）→ 次の DONE ペインを試す

    # tmux セッションにペインを追加（DONE ペインを再利用しなかった場合）
    if respawned:
        pass  # respawn 済み、pane_id は設定済み
    elif tmux_has_session(tmux_session):
        # mkdir はアトミック操作 - 最初の1つだけが成功する
        try:
            os.mkdir(first_agent_lock)
            # 最初の sub agent: 待機ペインを置き換え（明示的ペイン ID で競合回避）
            pane_id = waiting_pane_id or get_current_pane_id(tmux_session)
            run_tmux("respawn-pane", "-t", pane_id, "-k", tail_cmd)
        except OSError:
            # 2つ目以降: split-window -P -F で新ペインの ID を取得
            # "no space for new pane" 対策: select-layout tiled 後にリトライ
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
        # SessionStart hook が動いていない場合のフォールバック
        run_tmux("new-session", "-d", "-s", tmux_session, tail_cmd)
        pane_id = get_current_pane_id(tmux_session)

    # ペインタイトルを設定（明示的なペイン ID 指定で競合回避）
    if pane_id:
        run_tmux("select-pane", "-t", pane_id, "-T", pane_title)
    else:
        # フォールバック: pane_id が取れなかった場合はセッション指定
        run_tmux("select-pane", "-t", tmux_session, "-T", pane_title)

    # agent_id -> pane 情報を保存（pane_id も含めて保存）
    pane_info_file = os.path.join(SESSION_INFO_DIR, f"{session_id}.pane-{agent_id}")
    try:
        os.makedirs(SESSION_INFO_DIR, exist_ok=True)
        with open(pane_info_file, "w") as f:
            f.write(f"{tmux_session}\n{pane_id}")
    except OSError:
        pass


if __name__ == "__main__":
    main()
