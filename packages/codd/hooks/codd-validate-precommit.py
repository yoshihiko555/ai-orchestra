#!/usr/bin/env python3
"""PreToolUse hook: `git commit` 実行前に `codd validate` を走らせる。

`.claude/config/codd/codd.yaml` の `hooks.validate_on_commit` で挙動を制御する
（Issue #95）。既定は `warn` のため、明示的に opt-in しなくても `git commit`
実行のたびに `codd validate` は実行される（非ブロックの警告表示のみ）:

- `off`: 何もしない（validate 自体を実行しない）
- `warn`（既定）: `codd validate` を実行し、error 検出時も commit を通しつつ
  警告のみ additionalContext で表示する
- `block`: error 検出時に commit をブロックする（exit 2）

validate 自体の実行に失敗・timeout した場合は fail-safe として commit をブロックしない。

`codd validate` サブプロセスの終了コードは 2 通りの異なる意味を持つ（T1: Issue #95
bot レビュー対応）:

- ``returncode == 1``: 整合性エラーを検出した（正常な validate 結果）。この場合のみ
  warn/block 分岐（`hooks.validate_on_commit` の設定）に流す。
- ``returncode`` がそれ以外の非ゼロ（例: 2 = scope glob 不正等の設定エラー）:
  validate 自体の実行失敗。block モードであっても commit をブロックしない
  （fail-safe。stderr に日本語 1 行で通知するのみ）。

`git -C <path> commit` のように `-C` でリポジトリ/worktree を切り替える呼び出しは、
hook の root（cwd）とは別ディレクトリを対象にしている可能性がある（T5: Issue #95
bot レビュー対応）。`-C` 解決後のディレクトリが hook の root 自身と一致しない場合は
ガード対象外として exit 0（安全側 skip）にする。`cd <path> && git commit` のような
複合コマンドでの `cd` 解析は行わない（root は hook 入力の cwd 固定という既知の
近似のまま）。
"""

from __future__ import annotations

import json
import os
import re
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

VALIDATE_TIMEOUT_SECONDS = 60

# `git` の後に `-C <path>` / `-c <key>=<value>` 等のグローバルオプションを挟む場合も
# 許容しつつ `commit` サブコマンドを検出する。素朴な `"git commit" in command` より
# 誤検出（例: `commit.py` や `git log` 単体）は少ないが、文字列中の偶然の一致
# （例: echo のリテラル文字列内）まで完全に排除することは意図しない。
_GIT_OPTION = r"(?:\s+-[A-Za-z](?:=\S+|\s+\S+)?|\s+--[\w-]+(?:=\S+)?)"
_GIT_COMMIT_PATTERN = re.compile(rf"\bgit(?:{_GIT_OPTION})*\s+commit(?=\s|$)")

# `-C <path>` / `-C=<path>` の値を抽出する（`git` 直後〜`commit` 直前のグローバル
# オプション区間のみに適用する。T5: Issue #95 bot レビュー対応）。
_DASH_C_PATTERN = re.compile(r"-C(?:=(\S+)|\s+(\S+))")


def _looks_like_git_commit(command: str) -> bool:
    """command 文字列が `git commit` サブコマンド呼び出しを含むかを判定する。"""
    return bool(_GIT_COMMIT_PATTERN.search(command))


def _extract_dash_c_paths(command: str) -> list[str]:
    """`git ... commit` 呼び出し内の `-C <path>` 値を出現順に抽出する（複数可）。

    `_GIT_COMMIT_PATTERN` がマッチした `git` 〜 `commit` の区間のみを対象にする。
    マッチしない場合は空リスト。
    """
    match = _GIT_COMMIT_PATTERN.search(command)
    if not match:
        return []
    segment = match.group(0)
    return [
        value for m in _DASH_C_PATTERN.finditer(segment) for value in (m.group(1) or m.group(2),)
    ]


def _resolve_dash_c_target(root: str, command: str) -> str:
    """`-C` を hook root 基準で順に解決した最終ディレクトリを返す（レキシカル正規化）。

    git は `-C` を複数回指定でき、2 個目以降は直前のディレクトリからの相対パスに
    なる。ここでは hook の root（= 検証対象のプロジェクトルート）を起点に同じ規約で
    順次結合する。`-C` が無い場合は root をそのまま返す。
    """
    current = root
    for path in _extract_dash_c_paths(command):
        current = os.path.normpath(os.path.join(current, path))
    return current


def _is_guard_target_root(root: str, command: str) -> bool:
    """`-C` 解決後のディレクトリが hook root 自身と一致するかを判定する。

    一致しない場合（別リポジトリ/worktree を指す `-C`）は、この hook のガード対象
    外として扱う（安全側 skip: 誤ガード・見逃しの両方を避けるため検証しない）。
    `-C` が無い場合は常に True（従来通りガード対象）。
    """
    return os.path.normpath(_resolve_dash_c_target(root, command)) == os.path.normpath(root)


def _resolve_project_root(data: dict) -> str:
    """プロジェクトルートを解決する（無ければ空文字）。"""
    return data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")


def _codd_config_path(root: str) -> Path:
    """`.claude/config/codd/codd.yaml`（同期後の実行時 config）のパスを返す。"""
    return Path(root) / ".claude" / "config" / "codd" / "codd.yaml"


def _run_validate(root: str) -> tuple[int, str, str] | None:
    """`codd validate` を実行し (exit_code, stdout, stderr) を返す。失敗・timeout 時は None。"""
    codd_cli = os.path.join(_orchestra_dir, "packages", "codd", "scripts", "codd.py")
    try:
        result = subprocess.run(
            ["python3", codd_cli, "validate"],
            cwd=root,
            timeout=VALIDATE_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[codd] validate failed: {exc}", file=sys.stderr)
        return None
    return result.returncode, result.stdout, result.stderr


def _extract_summary_line(stdout: str) -> str:
    """`[codd validate] errors=N warnings=M` のサマリー行を抽出する（無ければ固定文言）。"""
    for line in stdout.splitlines():
        if line.startswith("[codd validate]"):
            return line
    return "[codd validate] サマリー行を取得できませんでした"


def _emit_warn(summary: str) -> None:
    """warn モード: commit を通しつつ additionalContext で警告する。"""
    context = (
        f"[codd] validate でエラーを検出しました。\n{summary}\n"
        "詳細は `orchex run codd codd -- validate` で確認してください。"
    )
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    }
    print(json.dumps(output))


def _emit_block(summary: str) -> None:
    """block モード: commit をブロックする（exit 2）。"""
    message = (
        f"[codd] validate でエラーを検出したため commit をブロックしました。\n{summary}\n"
        "詳細は `orchex run codd codd -- validate` で確認してください。\n"
        "この検証を緩和するには `hooks.validate_on_commit` を "
        "`warn` または `off` に変更してください（config-loading ルール参照）。"
    )
    print(message, file=sys.stderr)
    sys.exit(2)


def _emit_execution_failure(returncode: int, stderr: str) -> None:
    """validate 実行失敗（returncode が 1 以外の非ゼロ。設定エラー等）を通知する。

    T1: Issue #95 bot レビュー対応。`returncode == 1`（整合性エラー検出）以外の
    非ゼロは validate 自体の実行失敗を意味する。`hooks.validate_on_commit` が
    `block` であっても commit をブロックしない（fail-safe）。stderr の 1 行目のみを
    添えて日本語で通知する。
    """
    stderr_head = stderr.splitlines()[0] if stderr.strip() else ""
    print(
        f"[codd] validate の実行に失敗しました（code={returncode}）: {stderr_head}\n"
        "設定エラーの可能性があります。`orchex run codd codd -- validate` で確認してください。",
        file=sys.stderr,
    )


@safe_hook_execution
def main() -> None:
    data = read_hook_input()

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not command or not _looks_like_git_commit(command):
        sys.exit(0)

    root = _resolve_project_root(data)
    if not root:
        sys.exit(0)

    if not _is_guard_target_root(root, command):
        sys.exit(0)  # -C が hook root 以外を指す → ガード対象外（安全側 skip、T5）

    config_path = _codd_config_path(root)
    if not config_path.is_file():
        sys.exit(0)  # codd 未初期化プロジェクト

    if not _orchestra_dir:
        sys.exit(0)

    ensure_package_path("codd", "lib")
    import codd_common as cc  # noqa: E402

    config = cc.load_config(config_path)
    if not config.enabled or config.hooks.validate_on_commit == cc.VALIDATE_ON_COMMIT_OFF:
        sys.exit(0)

    outcome = _run_validate(root)
    if outcome is None:
        sys.exit(0)  # fail-safe: validate 実行自体の失敗では commit をブロックしない

    exit_code, stdout, stderr = outcome
    if exit_code == 0:
        sys.exit(0)

    if exit_code != 1:
        # T1: returncode==1（整合性エラー検出）以外の非ゼロは validate 実行自体の
        # 失敗（設定エラー等）。block モードであっても commit をブロックしない。
        _emit_execution_failure(exit_code, stderr)
        sys.exit(0)

    summary = _extract_summary_line(stdout)
    if config.hooks.validate_on_commit == cc.VALIDATE_ON_COMMIT_BLOCK:
        _emit_block(summary)
    else:
        _emit_warn(summary)
    sys.exit(0)


if __name__ == "__main__":
    main()
