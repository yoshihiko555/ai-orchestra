#!/usr/bin/env python3
"""hooks 間で共有する汎用ユーティリティ関数。"""

from __future__ import annotations

import functools
import json
import os
import sys
from collections.abc import Callable
from typing import Any


def deep_merge(base: dict, override: dict) -> dict:
    """override の値で base を再帰的に上書きする。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def find_package_config(package_name: str, filename: str, project_dir: str) -> str:
    """パッケージ config パスを解決する。

    探索順:
    1. {project_dir}/.claude/config/{package_name}/{filename}
    2. $AI_ORCHESTRA_DIR/packages/{package_name}/config/{filename}
    """
    project_path = os.path.join(project_dir, ".claude", "config", package_name, filename)
    if os.path.isfile(project_path):
        return project_path

    orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
    if orchestra_dir:
        orchestra_path = os.path.join(orchestra_dir, "packages", package_name, "config", filename)
        if os.path.isfile(orchestra_path):
            return orchestra_path

    return ""


def _read_config_file(path: str) -> dict:
    """拡張子に応じて JSON または YAML を読み込む。失敗時は空辞書を返す。"""
    if not path or not os.path.isfile(path):
        return {}
    ext = os.path.splitext(path)[1].lower()
    if ext in (".yaml", ".yml"):
        try:
            import yaml

            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}
    return read_json_safe(path)


def load_package_config(package_name: str, filename: str, project_dir: str) -> dict:
    """パッケージ config を読み込み、.local.{ext} があればマージする。"""
    base_path = find_package_config(package_name, filename, project_dir)
    if not base_path:
        return {}

    base = _read_config_file(base_path)

    # .local.{ext} をベースと同じディレクトリから探す
    name, ext = os.path.splitext(filename)
    local_filename = f"{name}.local{ext}"
    local_path = os.path.join(os.path.dirname(base_path), local_filename)
    local = _read_config_file(local_path)

    if local:
        return deep_merge(base, local)
    return base


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
# sys.path ヘルパー
# ---------------------------------------------------------------------------


def ensure_package_path(package_name: str, subdir: str = "hooks") -> None:
    """$AI_ORCHESTRA_DIR/packages/{package_name}/{subdir} を sys.path に追加する。"""
    orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
    if not orchestra_dir:
        return
    path = os.path.join(orchestra_dir, "packages", package_name, subdir)
    if path not in sys.path:
        sys.path.insert(0, path)


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
