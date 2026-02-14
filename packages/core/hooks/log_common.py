#!/usr/bin/env python3
"""統一イベントログの共通ライブラリ。

全フックが events.jsonl に統一フォーマットで書き込むための関数群。
既存の tmux_common.py (tmux 関連) とは責務を分離。
"""

from __future__ import annotations

import datetime
import json
import os


def find_project_root(start_dir: str | None = None) -> str:
    """プロジェクトルートを探す。.claude/ ディレクトリを持つ最寄りの親。"""
    current = start_dir or os.getcwd()
    for parent in [current, *_parents(current)]:
        if os.path.isdir(os.path.join(parent, ".claude")):
            return parent
    return current


def _parents(path: str) -> list[str]:
    parts: list[str] = []
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            break
        parts.append(parent)
        path = parent
    return parts


def get_events_log_path(project_dir: str | None = None) -> str:
    """events.jsonl のフルパスを返す。"""
    root = project_dir or find_project_root()
    return os.path.join(root, ".claude", "logs", "orchestration", "events.jsonl")


def truncate_text(text: str, max_length: int = 2000) -> str:
    """長いテキストを切り詰める。"""
    if len(text) <= max_length:
        return text
    return text[:max_length] + f"... [{len(text)} chars]"


def append_jsonl(path: str, record: dict) -> None:
    """JSONL ファイルに1行追記する。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_event(
    event_type: str,
    data: dict,
    *,
    session_id: str = "",
    hook_name: str = "",
    project_dir: str | None = None,
) -> None:
    """events.jsonl に統一フォーマットのイベントを追記する。

    レコード形式:
    {
        "timestamp": "ISO8601",
        "session_id": "...",
        "event_type": "session_start",
        "hook": "session-start",
        "data": { ... }
    }
    """
    record = {
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        "session_id": session_id,
        "event_type": event_type,
        "hook": hook_name,
        "data": data,
    }
    path = get_events_log_path(project_dir)
    append_jsonl(path, record)
