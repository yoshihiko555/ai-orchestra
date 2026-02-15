#!/usr/bin/env python3
"""hooks 間で共有する汎用ユーティリティ関数。"""

from __future__ import annotations

import functools
import json
import os
import sys
from typing import Any, Callable


def read_hook_input() -> dict:
    """stdin から JSON を読み取って dict を返す。"""
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return {}


def get_field(data: dict, key: str) -> str:
    """dict からフィールドを取得する。存在しなければ空文字を返す。"""
    return data.get(key) or ""


# ---------------------------------------------------------------------------
# JSON ファイル操作
# ---------------------------------------------------------------------------


def read_json_safe(path: str) -> dict:
    """JSON ファイルを読み込み、失敗時は空辞書を返す。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {}


def write_json(path: str, data: dict) -> None:
    """dict を JSON ファイルに書き出す。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_jsonl(path: str, record: dict) -> None:
    """dict を JSONL ファイルに追記する。"""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# ネスト構造からの値検索
# ---------------------------------------------------------------------------


def find_first_text(node: Any, keys: set[str]) -> str:
    """ネストされた dict/list から keys に一致する最初の非空文字列を返す。"""
    if isinstance(node, dict):
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in node.values():
            found = find_first_text(value, keys)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_first_text(item, keys)
            if found:
                return found
    return ""


def find_first_int(node: Any, keys: set[str]) -> int | None:
    """ネストされた dict/list から keys に一致する最初の整数値を返す。"""
    if isinstance(node, dict):
        for key in keys:
            value = node.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                try:
                    return int(value)
                except ValueError:
                    pass
        for value in node.values():
            found = find_first_int(value, keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_first_int(item, keys)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------------------
# エラーハンドリング
# ---------------------------------------------------------------------------


def safe_hook_execution(func: Callable[[], None]) -> Callable[[], None]:
    """Hook の main() を安全にラップし、例外時は stderr にログ出力して exit(0) する。"""

    @functools.wraps(func)
    def wrapper() -> None:
        try:
            func()
        except Exception as e:
            print(f"Hook error ({func.__module__}): {e}", file=sys.stderr)
            sys.exit(0)

    return wrapper


# ---------------------------------------------------------------------------
# 統一イベントログ
# ---------------------------------------------------------------------------


def try_append_event(
    event_type: str,
    data: dict,
    *,
    session_id: str = "",
    hook_name: str = "",
    project_dir: str = "",
) -> None:
    """統一イベントログへの追記を試みる。失敗しても例外を上げない。"""
    try:
        _orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
        if not _orchestra_dir:
            return
        _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
        if _core_hooks not in sys.path:
            sys.path.insert(0, _core_hooks)
        from log_common import append_event

        append_event(
            event_type,
            data,
            session_id=session_id,
            hook_name=hook_name,
            project_dir=project_dir,
        )
    except Exception:
        pass
