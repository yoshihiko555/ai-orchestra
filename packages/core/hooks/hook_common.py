#!/usr/bin/env python3
"""hooks 間で共有する汎用ユーティリティ関数。"""

from __future__ import annotations

import functools
import json
import os
import stat
import sys
from collections.abc import Callable
from typing import Any

# CLI ツール設定（cli-tools.yaml）が読めない場合のフォールバック既定値（SSOT）。
#
# 正本は常に cli-tools.yaml。これらは config が「読めない／キーが無い」障害時のみ
# 使われる最終安全網であり、各 hook がここを参照することで既定値の散在を防ぐ。
#
# 重要: これらは cli-tools.yaml と意図的に独立しており、yaml のモデルを変更しても
# ここを同期する必要はない（同期を強制するテストも置かない）。役割は「正本が読めない
# 障害時にとにかく何か動く値を返す」こと。値が多少古くても安全網としては許容する。
DEFAULT_CODEX_MODEL = "gpt-5.5"
DEFAULT_CODEX_SANDBOX_ANALYSIS = "read-only"
DEFAULT_CODEX_FLAGS = "--full-auto"
# 空文字 = --model フラグを省略し、Antigravity CLI のデフォルトモデルに委ねる意図。
DEFAULT_ANTIGRAVITY_MODEL = ""
DEFAULT_ANTIGRAVITY_FLAGS = ""


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
    """パッケージ config を読み込み、.local.{ext} があればマージする。

    local override の探索順:
    1. {project_dir}/.claude/config/{package_name}/{name}.local.{ext}
    2. base_path と同じディレクトリ（フォールバック）
    """
    base_path = find_package_config(package_name, filename, project_dir)
    if not base_path:
        return {}

    base = _read_config_file(base_path)

    name, ext = os.path.splitext(filename)
    local_filename = f"{name}.local{ext}"

    # プロジェクトディレクトリを優先的に検索
    project_local = os.path.join(project_dir, ".claude", "config", package_name, local_filename)
    if os.path.isfile(project_local):
        local = _read_config_file(project_local)
    else:
        # フォールバック: base_path と同じディレクトリ
        local_path = os.path.join(os.path.dirname(base_path), local_filename)
        local = _read_config_file(local_path)

    if local:
        return deep_merge(base, local)
    return base


def normalize_cli_tools_config(config: dict) -> dict:
    """cli-tools.yaml の旧 gemini 系設定を antigravity に正規化する。

    横展開先プロジェクトの .local.yaml に残る旧キーへの後方互換:

    1. トップレベル ``gemini:`` キーの ``enabled: false`` は
       ``antigravity.enabled`` に反映する（無効化の意図を引き継ぐ。
       ``model`` / ``flags`` は Gemini CLI 固有値のため引き継がない）
    2. ``agents.<name>.tool: "gemini"`` は ``"antigravity"`` に読み替える

    Args:
        config: load_package_config が返した cli-tools.yaml の dict。

    Returns:
        正規化済みの新しい dict（入力は変更しない）。
    """
    if not isinstance(config, dict):
        return config

    normalized = dict(config)

    legacy = normalized.get("gemini")
    if isinstance(legacy, dict) and legacy.get("enabled") is False:
        antigravity = dict(normalized.get("antigravity") or {})
        antigravity["enabled"] = False
        normalized["antigravity"] = antigravity

    agents = normalized.get("agents")
    if isinstance(agents, dict):
        new_agents: dict = {}
        for name, cfg in agents.items():
            if isinstance(cfg, dict) and cfg.get("tool") == "gemini":
                cfg = {**cfg, "tool": "antigravity"}
            new_agents[name] = cfg
        normalized["agents"] = new_agents

    return normalized


def is_cli_enabled(cli_name: str, config: dict) -> bool:
    """CLI が有効かどうかを返す。未定義やセクション欠落時は True（後方互換）。

    元々 agent-routing パッケージが所有していたが、codex-suggestions /
    antigravity-suggestions など agent-routing に依存しないパッケージからも
    利用するため core に引き上げた。route_config.is_cli_enabled はここからの
    re-export として後方互換を維持する。
    """
    section = config.get(cli_name, {})
    if not isinstance(section, dict):
        return True
    return bool(section.get("enabled", True))


def read_hook_input() -> dict:
    """stdin から JSON を読み取って dict を返す。"""
    try:
        result = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def resolve_path_within(project_dir: str, relative: str, filename: str) -> str | None:
    """relative + filename を project_dir 配下に解決する。

    config 値経由の `relative`（例: logs_dir）に `../` 等が含まれ project_dir
    の外を指す場合は None を返す（設定経由のパストラバーサル防御）。
    symlink による脱出も realpath 解決で検出する。
    """
    project_root = os.path.realpath(project_dir)
    candidate = os.path.realpath(os.path.join(project_dir, relative, filename))
    if candidate == project_root or candidate.startswith(project_root + os.sep):
        return candidate
    return None


def get_field(data: dict, key: str) -> str:
    """dict からフィールドを取得する。存在しなければ空文字を返す。"""
    return str(data.get(key) or "")


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
    """dict を JSON ファイルにアトミックに書き出す。"""
    tmp_path = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            existing_mode = stat.S_IMODE(os.stat(path).st_mode)
            os.chmod(tmp_path, existing_mode)
        except FileNotFoundError:
            pass  # 新規作成時はデフォルト（umask）のまま
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


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
