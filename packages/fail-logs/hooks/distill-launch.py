#!/usr/bin/env python3
"""SessionEnd hook: 教訓化のワーカーを切り離して起動する（設計 docs/design/fail-logs.md §7.1・§7.7）。

設定の読み込み・環境変数の確認・spool のディレクトリの有無の確認だけを行い、git・flock・セッションの記録は使わない。
起動の条件に当たらなければ何もせずに戻る（実行記録も書かない）。どの場合も無出力・終了コード 0 で、すぐ戻る。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_hook_dir = os.path.dirname(os.path.abspath(__file__))
_lib_dir = os.path.abspath(os.path.join(_hook_dir, "..", "lib"))
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
for _candidate in (
    os.path.join(_orchestra_dir, "packages", "core", "hooks") if _orchestra_dir else "",
    os.path.abspath(os.path.join(_hook_dir, "..", "..", "core", "hooks")),
):
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

try:
    from hook_common import (
        load_package_config,
        read_hook_input,
        resolve_path_within,
        safe_hook_execution,
    )

    _AVAILABLE = True
except Exception:  # noqa: BLE001 - 部品が欠けてもセッションの終了を止めない
    _AVAILABLE = False

WORKER_PATH = os.path.join(_lib_dir, "distill_worker.py")
DEFAULT_LOGS_DIR = os.path.join(".claude", "logs", "fail-logs")
SPOOL_DIR = "spool"
DISTILL_ENV = "FAIL_LOGS_DISTILL"
NONINTERACTIVE_PREFIX = "sdk-"
MODE_DISTILL = "distill"
MODE_IMPORT_ONLY = "import_only"
_popen = subprocess.Popen  # テストで差し替える（subprocess モジュール全体は差し替えない）


def _resolve_project_dir(data: dict) -> str:
    cwd = str(data.get("cwd") or "")
    if cwd and os.path.isdir(os.path.join(cwd, ".claude")):
        return cwd
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _distill_enabled(config: dict) -> bool:
    """教訓化を起動するか（有効・教訓化セッションの外・対話、または非対話を含める設定）。"""
    distill = config.get("distill") if isinstance(config.get("distill"), dict) else {}
    if distill.get("enabled") is not True:
        return False
    if os.environ.get(DISTILL_ENV):
        return False
    noninteractive = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith(NONINTERACTIVE_PREFIX)
    return not noninteractive or distill.get("include_noninteractive") is True


def _has_spool(project_dir: str, config: dict) -> bool:
    """現在のプロジェクトの spool に取り込むものがあるか（中身は読まない）。"""
    logs_dir = (
        config.get("logs_dir") if isinstance(config.get("logs_dir"), str) else DEFAULT_LOGS_DIR
    )
    path = resolve_path_within(project_dir, logs_dir or DEFAULT_LOGS_DIR, SPOOL_DIR)
    if path is None or os.path.islink(path) or not os.path.isdir(path):
        return False
    try:
        with os.scandir(path) as entries:
            return any(entry.name.endswith(".json") for entry in entries)
    except OSError:
        return False


def _decide_mode(project_dir: str, config: dict) -> str | None:
    if config.get("enabled", True) is False:
        return None
    if _distill_enabled(config):
        return MODE_DISTILL
    if os.environ.get(DISTILL_ENV):
        return None
    return MODE_IMPORT_ONLY if _has_spool(project_dir, config) else None


def _launch(request: dict) -> None:
    """ワーカーを、hook の標準入出力を引き継がずに切り離して起動し、標準入力で依頼を渡す。"""
    try:
        process = _popen(
            [sys.executable, WORKER_PATH],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        if process.stdin is None:
            return
        process.stdin.write(json.dumps(request).encode("utf-8"))
        process.stdin.close()
    except OSError:
        return  # ワーカーが起動直後に落ちた（BrokenPipe 等）。セッションの終了には影響させない


def _run() -> None:
    data = read_hook_input()
    project_dir = _resolve_project_dir(data)
    config = load_package_config("fail-logs", "fail-logs.yaml", project_dir)
    mode = _decide_mode(project_dir, config)
    if mode is None or not os.path.isfile(WORKER_PATH):
        return
    _launch(
        {
            "mode": mode,
            "session_id": str(data.get("session_id") or ""),
            "transcript_path": str(data.get("transcript_path") or ""),
            "project_dir": project_dir,
        }
    )


def main() -> None:
    """hook のエントリポイント。部品が読めないときは何もせずに戻る。"""
    if not _AVAILABLE:
        return
    safe_hook_execution(_run)()


if __name__ == "__main__":
    main()
