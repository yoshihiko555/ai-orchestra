#!/usr/bin/env python3
"""統一イベントログライブラリ（audit package v1 スキーマ）。

セッション単位のローテーションで .claude/logs/audit/sessions/{session_id}.jsonl に
イベントを書き出す。全 audit hook はこのモジュール経由でログを記録する。
"""

from __future__ import annotations

import datetime
import json
import os
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
    }
)


# ---------------------------------------------------------------------------
# ID Generation
# ---------------------------------------------------------------------------


def generate_id() -> str:
    """イベント ID / トレース ID 用の短縮 UUID を生成する。"""
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Path Resolution
# ---------------------------------------------------------------------------


def _resolve_project_dir(project_dir: str | None = None) -> str:
    """プロジェクトルートを解決する。"""
    if project_dir:
        return project_dir
    # .claude/ ディレクトリを持つ最寄りの親を探索
    current = os.getcwd()
    while True:
        if os.path.isdir(os.path.join(current, ".claude")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.getcwd()
        current = parent


def _sanitize_session_id(session_id: str) -> str:
    """session_id をファイル名として安全な形に正規化する。"""
    import re

    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)
    return sanitized[:64] if sanitized else "unknown"


def get_session_log_path(session_id: str, project_dir: str | None = None) -> str:
    """セッション単位の JSONL ログパスを返す。"""
    root = _resolve_project_dir(project_dir)
    safe_id = _sanitize_session_id(session_id)
    return os.path.join(root, SESSIONS_DIR, f"{safe_id}.jsonl")


def get_log_base_path(project_dir: str | None = None) -> str:
    """ログベースディレクトリのパスを返す。"""
    root = _resolve_project_dir(project_dir)
    return os.path.join(root, LOG_BASE_DIR)


# ---------------------------------------------------------------------------
# Trace State (hook 間でトレース ID を受け渡す)
# ---------------------------------------------------------------------------


def _trace_state_path(project_dir: str | None = None) -> str:
    root = _resolve_project_dir(project_dir)
    return os.path.join(root, STATE_DIR, TRACE_STATE_FILE)


def _atomic_write_json(path: str, data: dict) -> None:
    """JSON ファイルをアトミックに書き出す（tempfile + os.replace）。"""
    import tempfile

    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, mode=LOG_DIR_MODE, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".audit-", dir=dir_name)
    try:
        with os.fdopen(fd, "w") as f:
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
    """現在のトレース ID を state ファイルに保存する（アトミック書き込み）。"""
    path = _trace_state_path(project_dir)
    data = {
        "tid": tid,
        "session_id": session_id,
        "expected_route": expected_route,
        "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    _atomic_write_json(path, data)


def load_trace_state(project_dir: str | None = None) -> dict[str, str]:
    """state ファイルからトレース情報を読み込む。存在しなければ空辞書。"""
    path = _trace_state_path(project_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Subagent Trace State (サブエージェント固有の trace ID を保持)
# ---------------------------------------------------------------------------


def _subagent_trace_path(agent_id: str, project_dir: str | None = None) -> str:
    root = _resolve_project_dir(project_dir)
    return os.path.join(root, STATE_DIR, f"audit-subagent-{agent_id}.json")


def save_subagent_trace(
    *,
    aid: str,
    tid: str,
    ptid: str = "",
    project_dir: str | None = None,
) -> None:
    """サブエージェント固有のトレース情報を保存する（アトミック書き込み）。"""
    path = _subagent_trace_path(aid, project_dir)
    data = {
        "tid": tid,
        "ptid": ptid,
        "aid": aid,
        "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    _atomic_write_json(path, data)


def load_subagent_trace(aid: str, project_dir: str | None = None) -> dict[str, str]:
    """サブエージェント固有のトレース情報を読み込む。存在しなければ空辞書。"""
    path = _subagent_trace_path(aid, project_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def cleanup_subagent_trace(aid: str, project_dir: str | None = None) -> None:
    """サブエージェント固有のトレース state ファイルを削除する。"""
    path = _subagent_trace_path(aid, project_dir)
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Event Emission
# ---------------------------------------------------------------------------


def _append_jsonl(path: str, record: dict) -> None:
    """JSONL ファイルに 1 行追記する（排他ロック + パーミッション制限）。"""
    import fcntl

    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, mode=LOG_DIR_MODE, exist_ok=True)

    is_new = not os.path.exists(path)
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    if is_new:
        try:
            os.chmod(path, LOG_FILE_MODE)
        except OSError:
            pass


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

    Returns:
        書き出したレコード（テスト用）。
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
    """セッション用ログディレクトリを初期化し、パスを返す。"""
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
            with open(path) as f:
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
    """存在するセッション ID の一覧を返す（時刻順）。"""
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
