#!/usr/bin/env python3
"""CLI 間コンテキスト共有基盤。

ファイルベースのストレージ（.claude/context/）を通じて
セッション内サブエージェント結果の集約と CLI 間の作業コンテキスト共有を行う。

ストレージ構造:
  .claude/context/
    session/
      meta.json          # セッション ID、開始時刻
      entries/           # サブエージェント結果（Map-Reduce）
        {agent_id}_{timestamp}.json
    shared/
      working-context.json  # 作業中ファイル・設計判断・フェーズ
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from hook_common import read_json_safe, write_json
except ImportError:
    # フォールバック: hook_common が import できない場合は直接 json を使う
    def read_json_safe(path: str) -> dict:  # type: ignore[misc]
        """JSON ファイルを読み込み、失敗時は空辞書を返す。"""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return {}

    def write_json(path: str, data: dict) -> None:  # type: ignore[misc]
        """dict を JSON ファイルに書き出す。"""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# modified_files リストの最大件数
MAX_MODIFIED_FILES = 100
_AGENT_ID_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_AGENT_ID_LEN = 64


def _context_dir(project_dir: str) -> str:
    """コンテキストディレクトリのベースパスを返す。"""
    return os.path.join(project_dir, ".claude", "context")


def _session_dir(project_dir: str) -> str:
    """セッションディレクトリのパスを返す。"""
    return os.path.join(_context_dir(project_dir), "session")


def _entries_dir(project_dir: str) -> str:
    """エントリーディレクトリのパスを返す。"""
    return os.path.join(_session_dir(project_dir), "entries")


def _shared_dir(project_dir: str) -> str:
    """共有ディレクトリのパスを返す。"""
    return os.path.join(_context_dir(project_dir), "shared")


def _now_iso8601() -> str:
    """現在時刻を ISO8601 形式で返す。"""
    return datetime.now(tz=UTC).isoformat()


def _sanitize_agent_id(agent_id: str) -> str:
    """agent_id をファイル名として安全な形式に正規化する。"""
    raw = str(agent_id or "").strip()
    safe = _AGENT_ID_SAFE_PATTERN.sub("-", raw)
    safe = re.sub(r"-{2,}", "-", safe).strip("-_")
    if not safe:
        return "unknown"
    return safe[:_MAX_AGENT_ID_LEN]


def get_project_dir(data: dict) -> str:
    """hook 入力からプロジェクトディレクトリを取得する。

    優先順位:
    1. data["cwd"]
    2. 環境変数 CLAUDE_PROJECT_DIR
    3. os.getcwd()
    """
    cwd = data.get("cwd") or ""
    if cwd:
        return cwd
    return os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def init_context_dir(project_dir: str) -> None:
    """コンテキストディレクトリを初期化する。

    `.claude/context/session/`、`.claude/context/session/entries/`、
    `.claude/context/shared/` ディレクトリを作成し、
    `session/meta.json` を生成する。既に存在する場合はスキップ（冪等）。

    Args:
        project_dir: プロジェクトのルートディレクトリパス。
    """
    try:
        session_dir = _session_dir(project_dir)
        entries_dir = _entries_dir(project_dir)
        shared_dir = _shared_dir(project_dir)

        Path(entries_dir).mkdir(parents=True, exist_ok=True)
        Path(shared_dir).mkdir(parents=True, exist_ok=True)

        meta_path = os.path.join(session_dir, "meta.json")
        if not os.path.isfile(meta_path):
            write_json(
                meta_path,
                {
                    "session_id": str(uuid.uuid4()),
                    "started_at": _now_iso8601(),
                },
            )
    except Exception as e:
        print(f"context_store.init_context_dir: {e}", file=sys.stderr)


def write_entry(project_dir: str, agent_id: str, data: dict[str, Any]) -> None:
    """サブエージェントの実行結果をエントリーファイルに書き出す。

    `session/entries/{agent_id}_{timestamp}.json` に data を書き出す。
    タイムスタンプを付加することで、同一 agent_id による複数回呼び出しでも
    上書きされずに全結果が保持される。
    data には `agent_id`、`task_name`、`timestamp`、`status`、`summary` が含まれることを想定する。

    Args:
        project_dir: プロジェクトのルートディレクトリパス。
        agent_id: サブエージェントの識別子。ファイル名に使用される。
        data: 書き出すデータ。
    """
    try:
        entries_dir = _entries_dir(project_dir)
        Path(entries_dir).mkdir(parents=True, exist_ok=True)

        safe_agent_id = _sanitize_agent_id(agent_id)
        timestamp_str = _now_iso8601().replace(":", "-").replace("+", "_")
        entry_path = str(Path(entries_dir).joinpath(f"{safe_agent_id}_{timestamp_str}.json"))
        write_json(entry_path, data)
    except Exception as e:
        print(f"context_store.write_entry: {e}", file=sys.stderr)


def read_entries(project_dir: str) -> list[dict[str, Any]]:
    """セッションエントリーの全データを読み込む。

    `session/entries/` 内の全 `.json` ファイルを読み込み、リストで返す。
    ファイルが存在しない、または空の場合は空リストを返す。

    Args:
        project_dir: プロジェクトのルートディレクトリパス。

    Returns:
        各エントリーファイルの内容を格納したリスト。
    """
    entries: list[dict[str, Any]] = []
    try:
        entries_dir = _entries_dir(project_dir)
        if not os.path.isdir(entries_dir):
            return entries

        for filename in sorted(os.listdir(entries_dir)):
            if not filename.endswith(".json"):
                continue
            entry_path = os.path.join(entries_dir, filename)
            data = read_json_safe(entry_path)
            if data:
                entries.append(data)
    except Exception as e:
        print(f"context_store.read_entries: {e}", file=sys.stderr)

    return entries


def update_working_context(project_dir: str, updates: dict[str, Any]) -> None:
    """作業コンテキストを更新する。

    `shared/working-context.json` を読み込み、updates で更新して書き出す。
    `modified_files` キーがリストの場合は既存リストに追加（重複排除）する。
    `modified_files` は最大 MAX_MODIFIED_FILES 件に制限する。
    `updated_at` は常に現在時刻（ISO8601）に更新される。
    ファイル書き込みは fcntl.flock による排他ロックで保護する。

    Args:
        project_dir: プロジェクトのルートディレクトリパス。
        updates: 更新内容を格納した辞書。
    """
    try:
        shared_dir = _shared_dir(project_dir)
        Path(shared_dir).mkdir(parents=True, exist_ok=True)

        context_path = os.path.join(shared_dir, "working-context.json")
        lock_path = context_path + ".lock"

        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                current = read_json_safe(context_path)

                merged: dict[str, Any] = {**current}
                for key, value in updates.items():
                    if key == "modified_files" and isinstance(value, list):
                        existing: list[str] = merged.get("modified_files", [])
                        if not isinstance(existing, list):
                            existing = []
                        combined = existing + [f for f in value if f not in existing]
                        merged["modified_files"] = combined[-MAX_MODIFIED_FILES:]
                    else:
                        merged[key] = value

                merged["updated_at"] = _now_iso8601()
                write_json(context_path, merged)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"context_store.update_working_context: {e}", file=sys.stderr)


def read_working_context(project_dir: str) -> dict[str, Any]:
    """作業コンテキストを読み込む。

    `shared/working-context.json` を読み込んで返す。
    ファイルが存在しない場合は空辞書を返す。

    Args:
        project_dir: プロジェクトのルートディレクトリパス。

    Returns:
        作業コンテキストの内容。存在しない場合は空辞書。
    """
    try:
        context_path = os.path.join(_shared_dir(project_dir), "working-context.json")
        return read_json_safe(context_path)
    except Exception as e:
        print(f"context_store.read_working_context: {e}", file=sys.stderr)
        return {}


def cleanup_session(project_dir: str) -> None:
    """セッションデータをクリーンアップする。

    `session/` ディレクトリと `shared/working-context.json` を削除する。
    ファイルが存在しない場合など、エラーは無視する。

    Args:
        project_dir: プロジェクトのルートディレクトリパス。
    """
    try:
        session_dir = _session_dir(project_dir)
        if os.path.isdir(session_dir):
            shutil.rmtree(session_dir)
    except Exception as e:
        print(f"context_store.cleanup_session (session): {e}", file=sys.stderr)

    try:
        context_path = os.path.join(_shared_dir(project_dir), "working-context.json")
        if os.path.isfile(context_path):
            os.remove(context_path)
        lock_path = context_path + ".lock"
        if os.path.isfile(lock_path):
            os.remove(lock_path)
    except Exception as e:
        print(f"context_store.cleanup_session (working-context): {e}", file=sys.stderr)
