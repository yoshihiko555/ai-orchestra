#!/usr/bin/env python3
"""InstructionsLoaded hook: CLAUDE.md / rules などの読み込みを監査ログに記録する。

Claude Code が instructions ファイル (CLAUDE.md, .claude/rules/*.md 等) を
ロードしたタイミングで発火し、どのファイルが・なぜロードされたかを
audit v1 ログに追記する。

目的:
- Codex Context Gap 問題の観測（どのルールがどの CLI に渡っているか可視化）
- デバッグ時にロード順序と load_reason を後追いできるようにする

副作用:
- stdout への JSON 出力は行わない（観測専用）。
"""

from __future__ import annotations

import os
import sys

_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if os.path.isdir(_core_hooks) and _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)
    _audit_hooks = os.path.join(_orchestra_dir, "packages", "audit", "hooks")
    if os.path.isdir(_audit_hooks) and _audit_hooks not in sys.path:
        sys.path.insert(0, _audit_hooks)

from event_logger import (
    emit_event,
    load_trace_state,
    resolve_project_root_from_hook_data,
)
from hook_common import (
    load_package_config,
    read_hook_input,
    safe_hook_execution,
)

# プロジェクトルートからの相対化を試みるベースパス候補
_PATH_KEYS = ("file_path", "trigger_file_path", "parent_file_path")


def _relativize(path: str, project_dir: str) -> str:
    """project_dir 起点の相対パスに変換する。範囲外なら元の文字列を返す。"""
    if not path:
        return path
    try:
        return os.path.relpath(path, project_dir)
    except ValueError:
        return path


def build_payload(data: dict, project_dir: str) -> dict:
    """instructions_loaded イベントのペイロードを組み立てる。

    Args:
        data: hook stdin 入力。
        project_dir: 解決済みプロジェクトルート。

    Returns:
        emit_event に渡すペイロード辞書。
    """
    payload: dict = {
        "load_reason": str(data.get("load_reason") or ""),
        "memory_type": str(data.get("memory_type") or ""),
    }
    for key in _PATH_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value:
            payload[key] = _relativize(value, project_dir)

    globs = data.get("globs")
    if isinstance(globs, list) and globs:
        payload["globs"] = [str(g) for g in globs[:10]]

    return payload


@safe_hook_execution
def main() -> None:
    """InstructionsLoaded hook のエントリポイント。"""
    data = read_hook_input()

    project_dir = resolve_project_root_from_hook_data(data)

    flags = load_package_config("audit", "audit-flags.json", project_dir)
    feature_cfg = (flags.get("features") or {}).get("instructions_loaded") or {}
    # デフォルトで有効。明示的に disabled にされた場合のみ停止する。
    if feature_cfg.get("enabled") is False:
        return

    session_id = str(data.get("session_id") or "")
    if not session_id:
        # セッション確立前（InstructionsLoaded は極めて早期に発火する）は書き込まない
        return

    payload = build_payload(data, project_dir)

    trace = load_trace_state(project_dir)
    tid = trace.get("tid") or ""

    emit_event(
        "instructions_loaded",
        payload,
        session_id=session_id,
        tid=tid,
        project_dir=project_dir,
    )


if __name__ == "__main__":
    main()
