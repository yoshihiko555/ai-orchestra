#!/usr/bin/env python3
"""統一イベントログライブラリ（audit package v1 スキーマ）。

セッション単位のローテーションで .claude/logs/audit/sessions/{session_id}.jsonl に
イベントを書き出す。全 audit hook はこのモジュール経由でログを記録する。
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import tempfile
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
# 以下のパス定数はプロジェクトルートとの相対パス。_resolve_project_dir() と結合して使用する
LOG_BASE_DIR = os.path.join(".claude", "logs", "audit")
SESSIONS_DIR = os.path.join(LOG_BASE_DIR, "sessions")
STATE_DIR = os.path.join(".claude", "state")
TRACE_STATE_FILE = "audit-trace.json"

# ログディレクトリ / state ファイルのパーミッション（所有者のみ読み書き可）
LOG_DIR_MODE = 0o700
LOG_FILE_MODE = 0o600

EVENT_TYPES = frozenset(
    {
        "session_start",
        "session_end",
        "prompt",
        "route_decision",
        "cli_call",
        "subagent_start",
        "subagent_end",
        "quality_gate",
        "instructions_loaded",
        "turn_end",
        "precompact",
    }
)


# ---------------------------------------------------------------------------
# ID Generation
# ---------------------------------------------------------------------------


def generate_id() -> str:
    """イベント ID / トレース ID 用の短縮 UUID を生成する。

    Returns:
        12 文字の 16 進文字列。
    """
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Path Resolution
# ---------------------------------------------------------------------------


def _resolve_project_dir(project_dir: str | None = None) -> str:
    """プロジェクトルートを解決する。

    Args:
        project_dir: 明示指定されたルート。None の場合は .claude/ を持つ親を探索。

    Returns:
        解決されたプロジェクトルートの絶対パス。見つからなければ CWD。
    """
    if project_dir:
        return project_dir
    current = os.getcwd()
    while True:
        if os.path.isdir(os.path.join(current, ".claude")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.getcwd()
        current = parent


def resolve_project_root_from_hook_data(data: dict) -> str:
    """hook 入力データからプロジェクトルートを解決する共通ヘルパー。

    Args:
        data: hook stdin から読み込んだ辞書（cwd フィールドを含む可能性）

    Returns:
        解決されたプロジェクトルート。data.cwd → CLAUDE_PROJECT_DIR → os.getcwd() の順。
    """
    cwd = str(data.get("cwd") or "")
    if cwd and os.path.isdir(os.path.join(cwd, ".claude")):
        return cwd
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _sanitize_session_id(session_id: str) -> str:
    """session_id をファイル名として安全な形に正規化する。

    Args:
        session_id: 外部入力の session_id。

    Returns:
        英数字・アンダースコア・ハイフンのみの文字列（最大 64 文字）。
        空になる場合は "unknown" を返す。
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)
    return sanitized[:64] if sanitized else "unknown"


def get_session_log_path(session_id: str, project_dir: str | None = None) -> str:
    """セッション単位の JSONL ログパスを返す。

    Args:
        session_id: セッション識別子。内部でサニタイズされる。
        project_dir: プロジェクトルート。省略時は自動解決。

    Returns:
        セッションログファイルの絶対パス。
    """
    root = _resolve_project_dir(project_dir)
    safe_id = _sanitize_session_id(session_id)
    return os.path.join(root, SESSIONS_DIR, f"{safe_id}.jsonl")


def get_log_base_path(project_dir: str | None = None) -> str:
    """ログベースディレクトリのパスを返す。

    Args:
        project_dir: プロジェクトルート。省略時は自動解決。

    Returns:
        `.claude/logs/audit` の絶対パス。
    """
    root = _resolve_project_dir(project_dir)
    return os.path.join(root, LOG_BASE_DIR)


# ---------------------------------------------------------------------------
# Trace State (hook 間でトレース ID を受け渡す)
# ---------------------------------------------------------------------------


def _trace_state_path(project_dir: str | None = None) -> str:
    """トレース state ファイルのパスを返す。"""
    root = _resolve_project_dir(project_dir)
    return os.path.join(root, STATE_DIR, TRACE_STATE_FILE)


def _atomic_write_json(path: str, data: dict) -> None:
    """JSON ファイルをアトミックに書き出す（tempfile + os.replace）。

    Args:
        path: 書き込み先パス。
        data: JSON 化する辞書。

    Raises:
        OSError: 書き込みに失敗した場合。
    """
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, mode=LOG_DIR_MODE, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".audit-", dir=dir_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(tmp_path, LOG_FILE_MODE)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def save_trace_state(
    tid: str,
    *,
    session_id: str = "",
    expected_route: str = "",
    project_dir: str | None = None,
) -> None:
    """現在のトレース ID を state ファイルに保存する（アトミック書き込み）。

    Args:
        tid: トレース ID（UserPromptSubmit 起点で生成される）
        session_id: セッション識別子
        expected_route: 予測ルート（audit-route.py が参照する）
        project_dir: プロジェクトルート。省略時は自動解決。
    """
    path = _trace_state_path(project_dir)
    data = {
        "tid": tid,
        "session_id": session_id,
        "expected_route": expected_route,
        "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    _atomic_write_json(path, data)


def load_trace_state(project_dir: str | None = None) -> dict[str, str]:
    """state ファイルからトレース情報を読み込む。

    Args:
        project_dir: プロジェクトルート。省略時は自動解決。

    Returns:
        トレース情報辞書。存在しない/パース失敗時は空辞書。
    """
    path = _trace_state_path(project_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Subagent Trace State (サブエージェント固有の trace ID を保持)
# ---------------------------------------------------------------------------


def _subagent_trace_path(agent_id: str, project_dir: str | None = None) -> str:
    """サブエージェント固有の state ファイルパスを返す。"""
    root = _resolve_project_dir(project_dir)
    safe_aid = _sanitize_session_id(agent_id)
    return os.path.join(root, STATE_DIR, f"audit-subagent-{safe_aid}.json")


def save_subagent_trace(
    *,
    aid: str,
    tid: str,
    ptid: str = "",
    project_dir: str | None = None,
) -> None:
    """サブエージェント固有のトレース情報を保存する（アトミック書き込み）。

    Args:
        aid: エージェント ID
        tid: サブエージェント固有のトレース ID
        ptid: 親トレース ID
        project_dir: プロジェクトルート。省略時は自動解決。
    """
    path = _subagent_trace_path(aid, project_dir)
    data = {
        "tid": tid,
        "ptid": ptid,
        "aid": aid,
        "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    _atomic_write_json(path, data)


def load_subagent_trace(aid: str, project_dir: str | None = None) -> dict[str, str]:
    """サブエージェント固有のトレース情報を読み込む。

    Args:
        aid: エージェント ID
        project_dir: プロジェクトルート。省略時は自動解決。

    Returns:
        トレース情報辞書。存在しない/パース失敗時は空辞書。
    """
    path = _subagent_trace_path(aid, project_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def cleanup_subagent_trace(aid: str, project_dir: str | None = None) -> None:
    """サブエージェント固有のトレース state ファイルを削除する。

    Args:
        aid: エージェント ID
        project_dir: プロジェクトルート。省略時は自動解決。
    """
    path = _subagent_trace_path(aid, project_dir)
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Event Emission
# ---------------------------------------------------------------------------


def _append_jsonl(path: str, record: dict) -> None:
    """JSONL ファイルに 1 行追記する（排他ロック + パーミッション制限、TOCTOU 耐性）。

    os.open で O_CREAT|O_APPEND フラグを指定することで、ファイル作成と
    パーミッション設定をアトミックに行う（stat → open の競合を排除）。

    Args:
        path: 書き込み先 JSONL ファイルのパス
        record: 書き込むイベント辞書
    """
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, mode=LOG_DIR_MODE, exist_ok=True)

    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        LOG_FILE_MODE,
    )
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def emit_event(
    event_type: str,
    data: dict[str, Any],
    *,
    session_id: str = "",
    tid: str = "",
    ptid: str | None = None,
    aid: str | None = None,
    ctx: dict[str, str | None] | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """統一スキーマ v1 のイベントを書き出す。

    Args:
        event_type: イベント種別（EVENT_TYPES のいずれか）
        data: イベント固有のペイロード
        session_id: セッション識別子（空の場合は書き込まずレコードのみ返す）
        tid: トレース ID。空の場合は新規生成。
        ptid: 親トレース ID（サブエージェント内なら設定）
        aid: エージェント ID（サブエージェント内なら設定）
        ctx: ワークフロー文脈辞書（skill / phase 等）
        project_dir: プロジェクトルート。省略時は自動解決。

    Returns:
        書き出したレコード辞書（テスト用）。

    Raises:
        ValueError: 未知の event_type が指定された場合。
    """
    if event_type not in EVENT_TYPES:
        msg = f"Unknown event_type: {event_type}"
        raise ValueError(msg)

    eid = generate_id()
    record = {
        "v": SCHEMA_VERSION,
        "ts": datetime.datetime.now(datetime.UTC).isoformat(),
        "sid": session_id,
        "eid": eid,
        "type": event_type,
        "tid": tid or generate_id(),
        "ptid": ptid,
        "aid": aid,
        "ctx": ctx or {"skill": None, "phase": None},
        "data": data,
    }

    if not session_id:
        return record

    path = get_session_log_path(session_id, project_dir)
    _append_jsonl(path, record)
    return record


# ---------------------------------------------------------------------------
# Session Lifecycle
# ---------------------------------------------------------------------------


def init_session_dir(session_id: str, project_dir: str | None = None) -> str:
    """セッション用ログディレクトリを初期化し、パスを返す。

    Args:
        session_id: セッション識別子
        project_dir: プロジェクトルート。省略時は自動解決。

    Returns:
        セッションログファイルの絶対パス。
    """
    root = _resolve_project_dir(project_dir)
    sessions_path = os.path.join(root, SESSIONS_DIR)
    os.makedirs(sessions_path, mode=LOG_DIR_MODE, exist_ok=True)
    try:
        os.chmod(sessions_path, LOG_DIR_MODE)
    except OSError:
        pass
    return get_session_log_path(session_id, project_dir)


# ---------------------------------------------------------------------------
# Log Reader API (スクリプトから使用)
# ---------------------------------------------------------------------------


def iter_session_events(
    project_dir: str | None = None,
    session_id: str | None = None,
) -> list[dict]:
    """セッションログからイベントを読み込む。

    Args:
        project_dir: プロジェクトルート（省略時は自動解決）
        session_id: 指定すると特定セッションのみ読む。省略時は全セッション

    Returns:
        v1 スキーマのイベントレコード一覧（時刻順）
    """
    root = _resolve_project_dir(project_dir)
    sessions_path = os.path.join(root, SESSIONS_DIR)
    if not os.path.isdir(sessions_path):
        return []

    events: list[dict] = []
    if session_id:
        files = [get_session_log_path(session_id, project_dir)]
    else:
        files = sorted(
            os.path.join(sessions_path, f)
            for f in os.listdir(sessions_path)
            if f.endswith(".jsonl")
        )

    for path in files:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue

    events.sort(key=lambda e: e.get("ts", ""))
    return events


def list_sessions(project_dir: str | None = None) -> list[str]:
    """存在するセッション ID の一覧を返す（時刻順）。

    Args:
        project_dir: プロジェクトルート。省略時は自動解決。

    Returns:
        セッション ID 文字列のリスト。ディレクトリが存在しなければ空リスト。
    """
    root = _resolve_project_dir(project_dir)
    sessions_path = os.path.join(root, SESSIONS_DIR)
    if not os.path.isdir(sessions_path):
        return []
    result = []
    for f in os.listdir(sessions_path):
        if f.endswith(".jsonl"):
            result.append(f[:-6])
    result.sort()
    return result
