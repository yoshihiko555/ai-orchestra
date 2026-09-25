#!/usr/bin/env python3
"""SessionStart hook: セッション用ログディレクトリを初期化し session_start を記録する。"""

from __future__ import annotations

import json
import os
import sys

_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)
    _audit_hooks = os.path.join(_orchestra_dir, "packages", "audit", "hooks")
    if _audit_hooks not in sys.path:
        sys.path.insert(0, _audit_hooks)

from event_logger import emit_event, generate_id, init_session_dir, save_trace_state
from hook_common import read_hook_input, safe_hook_execution
from log_common import find_project_root

# 案内専用の一覧。quality-gates.json の features / paths と対応する。
MOVED_FEATURE_KEYS = ("quality_gate", "context_optimization", "evaluation_set_check")
MOVED_PATH_KEYS = ("state_dir",)
AUDIT_LOCAL_FILENAME = "audit-flags.local.json"
AUDIT_BASE_FILENAME = "audit-flags.json"
MIGRATION_TARGET = ".claude/config/quality-gates/quality-gates.local.json"


def _collect_moved_keys(config: dict) -> list[str]:
    """audit-flags 形式の dict から移動済みキーを列挙する。

    features.* は dict 値、paths.* は str 値のみを対象とする（誤検出防止）。
    """
    features = config.get("features")
    paths = config.get("paths")
    moved_keys: list[str] = []
    if isinstance(features, dict):
        moved_keys.extend(
            f"features.{key}" for key in MOVED_FEATURE_KEYS if isinstance(features.get(key), dict)
        )
    if isinstance(paths, dict):
        moved_keys.extend(
            f"paths.{key}" for key in MOVED_PATH_KEYS if isinstance(paths.get(key), str)
        )
    return moved_keys


def find_moved_quality_gates_keys(project_dir: str) -> dict[str, list[str]]:
    """Issue #153 で所有分離した quality-gates の旧キーを検出する。

    {ファイル名: ["features.quality_gate", "paths.state_dir", ...]} を返す。
    audit-flags.local.json と audit-flags.json は、どちらも案内の検出だけに使う。
    読み込み失敗時は空扱いにする（fail-open）。
    """
    config_dir = os.path.join(project_dir, ".claude", "config", "audit")
    moved_keys_by_file: dict[str, list[str]] = {}

    for filename in (AUDIT_LOCAL_FILENAME, AUDIT_BASE_FILENAME):
        config_path = os.path.join(config_dir, filename)
        try:
            with open(config_path, encoding="utf-8") as config_file:
                config = json.load(config_file)
        except (json.JSONDecodeError, OSError, UnicodeError):
            continue

        if not isinstance(config, dict):
            continue

        moved_keys = _collect_moved_keys(config)
        if moved_keys:
            moved_keys_by_file[filename] = moved_keys

    return moved_keys_by_file


def build_migration_notice(moved_keys_by_file: dict[str, list[str]]) -> str:
    """対象ファイルと移動済みキーをまとめた 1 行の案内文を組み立てる。"""
    filenames: list[str] = []
    moved_keys: list[str] = []
    seen_keys: set[str] = set()

    for filename in (AUDIT_LOCAL_FILENAME, AUDIT_BASE_FILENAME):
        keys = moved_keys_by_file.get(filename)
        if not keys:
            continue
        filenames.append(filename)
        for key in keys:
            if key in seen_keys:
                continue
            seen_keys.add(key)
            moved_keys.append(key)

    return (
        f"[audit] {', '.join(filenames)} に quality-gates へ移動した設定が残っています"
        f"（{', '.join(moved_keys)}）。この値は読み込まれません。{MIGRATION_TARGET} へ移してください"
    )


@safe_hook_execution
def main() -> None:
    """SessionStart hook のエントリポイント。

    セッションログディレクトリを初期化し、初期トレース ID を生成して
    state ファイルに保存する。その後 session_start イベントを記録する。
    """
    data = read_hook_input()
    session_id = str(data.get("session_id") or "")
    if not session_id:
        return

    cwd = str(data.get("cwd") or "") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    init_session_dir(session_id, project_dir=cwd)

    # セッション開始時にトレース ID を生成して保存
    # 後続 hook が load_trace_state() で参照する
    initial_tid = generate_id()
    save_trace_state(initial_tid, session_id=session_id, project_dir=cwd)

    packages: list[str] = []
    orchestra_path = os.path.join(cwd, ".claude", "orchestra.json")
    if os.path.exists(orchestra_path):
        try:
            with open(orchestra_path, encoding="utf-8") as f:
                orchestra = json.load(f)
            packages = orchestra.get("installed_packages", [])
        except (json.JSONDecodeError, OSError):
            pass

    emit_event(
        "session_start",
        {"packages": packages},
        session_id=session_id,
        tid=initial_tid,
        project_dir=cwd,
    )

    # 案内の検出だけは .claude/ を持つ project root に正規化する（サブディレクトリ起動対策）。
    moved_keys_by_file = find_moved_quality_gates_keys(find_project_root(cwd))
    if not moved_keys_by_file:
        return

    print(build_migration_notice(moved_keys_by_file))


if __name__ == "__main__":
    main()
