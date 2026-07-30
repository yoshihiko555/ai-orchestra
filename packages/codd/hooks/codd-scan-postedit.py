#!/usr/bin/env python3
"""PostToolUse hook: scope 内ファイル編集後に `codd scan` で依存グラフを再構築する。

`.claude/config/codd/codd.yaml` の `hooks.scan_on_edit` で挙動を制御する（Issue #95）:

- 既定 `false`（opt-in）: 明示的に有効化しない限り `codd scan` は実行されない
- `true`: scope 内ファイルの Edit/Write 後に `codd scan` を実行する

hook の「登録」は manifest 経由で全導入先に自動展開されるが、`scan_on_edit` を
明示的に opt-in しない限り「実動作」（scan 実行）は発生しない。

いかなる場合もこの hook はツール呼び出しをブロックしない（常に exit 0）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

from hook_common import ensure_package_path, read_hook_input, safe_hook_execution  # noqa: E402

SCAN_TIMEOUT_SECONDS = 60


def _resolve_project_root(data: dict) -> str:
    """プロジェクトルートを解決する（無ければ空文字）。"""
    return data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")


def _codd_config_path(root: str) -> Path:
    """`.claude/config/codd/codd.yaml`（同期後の実行時 config）のパスを返す。"""
    return Path(root) / ".claude" / "config" / "codd" / "codd.yaml"


def _run_scan(root: str) -> None:
    """`codd scan` をサブプロセスで実行する（失敗しても hook はブロックしない）。

    サブプロセスの起動自体に失敗した場合（timeout / OSError）だけでなく、
    `codd scan` が実行はされたものの非ゼロ終了した場合（設定不正等）も、
    黙って握り潰さず stderr に 1 行だけ通知する（Medium-1: codd-review）。
    ただし exit 0 は維持し、ツール呼び出しはブロックしない。
    """
    codd_cli = os.path.join(_orchestra_dir, "packages", "codd", "scripts", "codd.py")
    try:
        result = subprocess.run(
            ["python3", codd_cli, "scan"],
            cwd=root,
            timeout=SCAN_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[codd] scan failed: {exc}", file=sys.stderr)
        return
    if result.returncode != 0:
        stderr_head = result.stderr.splitlines()[0] if result.stderr.strip() else ""
        print(
            f"[codd] scan がエラー終了しました（code={result.returncode}）: {stderr_head}",
            file=sys.stderr,
        )


@safe_hook_execution
def main() -> None:
    data = read_hook_input()

    if data.get("tool_name") not in ("Edit", "Write"):
        sys.exit(0)

    tool_input = data.get("tool_input")
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not file_path:
        sys.exit(0)

    root = _resolve_project_root(data)
    if not root:
        sys.exit(0)

    config_path = _codd_config_path(root)
    if not config_path.is_file():
        sys.exit(0)  # codd 未初期化プロジェクト

    if not _orchestra_dir:
        sys.exit(0)

    ensure_package_path("codd", "lib")
    import codd_common as cc  # noqa: E402

    config = cc.load_config(config_path)
    if not config.enabled or not config.hooks.scan_on_edit:
        sys.exit(0)

    target = Path(file_path)
    if not target.is_absolute():
        target = Path(root) / target

    if not cc.path_in_scan_scope(Path(root), target, config):
        sys.exit(0)

    _run_scan(root)
    sys.exit(0)


if __name__ == "__main__":
    main()
