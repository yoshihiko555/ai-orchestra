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

**index スナップショット検証（Issue #338）**: `git commit` が実際にコミットするのは
working tree ではなく **git index** の内容である。この hook は `git ... checkout-index`
で index の内容を一時ディレクトリへ展開し、その一時ディレクトリに対して `codd validate`
を実行する（実体の working tree・index は一切変更しない）。これにより「壊れた依存を
`git add` した後、同じファイルを未ステージで修正する」ケースでも、実際にコミットされる
内容（index）を正しく検証できる。index スナップショットを構築できない場合（対象が git
working tree でない、index に unmerged エントリがある、subprocess の timeout/OSError 等）は、
validate 実行自体の失敗と同様に fail-safe で commit をブロックしない。

**スナップショットへの git コンテキスト伝播（Issue #338 反復2）**: 一時ディレクトリは
`.git` を持たないため、素朴に `codd validate` を実行すると codd の drift 検査
（`_check_drift` / `batch_commit_times`）内の `git status` / `git log` が全て失敗し、
本来の commit 履歴ではなく checkout-index 実行時の mtime（ほぼ同時・パス順で書き込まれる
ため実際の履歴と無関係）へ黙ってフォールバックしてしまう。これにより「上流が下流より
新しい」drift を見逃す（false negative）副作用があった。これを解消するため、`codd
validate` サブプロセスには実リポジトリの絶対 git-dir（`git rev-parse --path-format=absolute
--git-dir` で解決）を `GIT_DIR`、一時ディレクトリを `GIT_WORK_TREE` として環境変数で渡す。
git はこれらの環境変数を優先するため、`codd validate` 内部の `git status` / `git log` は
「実リポジトリの履歴」を「index から checkout したスナップショットの内容」と突き合わせて
判定するようになり、drift 検査は working tree 直接検証時と同等の精度を保つ（設計判断は
`docs/design/codd-coherence-layer.md` §4.8.1 参照）。git-dir を解決できない場合はスナップ
ショット構築自体の失敗として扱い、fail-safe で commit をブロックしない。

**ambient GIT_* 環境変数のサニタイズ**: この hook が起動する git / `codd validate`
サブプロセスは、`hook_common.sanitized_git_env()` で ambient な `GIT_DIR` /
`GIT_WORK_TREE` 等を除去した環境変数を使う。loop-harness 等の外側の実行環境で
`GIT_DIR`/`GIT_WORK_TREE` が既に設定されているケース（ephemeral git isolation）でこれらを
継承すると、`write-tree` / `checkout-index` が `root`（検証対象のプロジェクト）とは無関係な
リポジトリを誤って参照してしまう（cwd より環境変数が優先されるため）。

**複合コマンドの既知の制限（Issue #338）**: PreToolUse hook は Bash コマンドが実行される
**前**に動作するため、`generate-docs && git add docs && git commit` のような複合コマンドで
は、hook 実行時点の index に同一コマンド内の先行ステップ（`git add` 等）の結果はまだ
反映されていない。`git ... commit` 呼び出しの直前に shell 連結演算子（`&&` / `;` / `||` /
`|`）を検出した場合は、warn/block メッセージにこの制限を注記する（ブロックはしない。
あくまで検証対象が「hook 実行時点の index」であることの明示）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

from hook_common import (  # noqa: E402
    ensure_package_path,
    read_hook_input,
    safe_hook_execution,
    sanitized_git_env,
)

VALIDATE_TIMEOUT_SECONDS = 60
# `git write-tree` / `git checkout-index` の subprocess timeout（Issue #338）。
INDEX_SNAPSHOT_TIMEOUT_SECONDS = 30

# `git` の後に `-C <path>` / `-c <key>=<value>` 等のグローバルオプションを挟む場合も
# 許容しつつ `commit` サブコマンドを検出する。素朴な `"git commit" in command` より
# 誤検出（例: `commit.py` や `git log` 単体）は少ないが、文字列中の偶然の一致
# （例: echo のリテラル文字列内）まで完全に排除することは意図しない。
_GIT_OPTION = r"(?:\s+-[A-Za-z](?:=\S+|\s+\S+)?|\s+--[\w-]+(?:=\S+)?)"
_GIT_COMMIT_PATTERN = re.compile(rf"\bgit(?:{_GIT_OPTION})*\s+commit(?=\s|$)")

# `-C <path>` / `-C=<path>` の値を抽出する（`git` 直後〜`commit` 直前のグローバル
# オプション区間のみに適用する。T5: Issue #95 bot レビュー対応）。
_DASH_C_PATTERN = re.compile(r"-C(?:=(\S+)|\s+(\S+))")

# `git ... commit` 呼び出しの直前が shell 連結演算子で終わっているかを検出する
# （複合コマンドの既知の制限を注記するため。Issue #338）。
_SHELL_CHAIN_OPERATOR_SUFFIX = re.compile(r"(?:&&|\|\||;|\|)\s*$")

_COMPOUND_COMMAND_NOTE = (
    "[codd] 注記: このコマンドは複合コマンド（`&&` 等）の一部として検出されました。"
    "validate は hook 実行時点（コマンド実行前）の git index を検証しており、"
    "同じコマンド内の `git add` 等それより前のステップの結果は反映されていません"
    "（PreToolUse hook はツール実行前に動作するため。既知の制限。設計書 §4.8.1 参照）。"
)


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


def _has_preceding_command_segment(command: str) -> bool:
    """`git ... commit` 呼び出しの直前に、別コマンドセグメントが連結されているかを判定する。

    `generate-docs && git add docs && git commit` のような複合コマンドでは、hook 実行時点
    （`git commit` 自体がまだ実行される前）の index には、同じコマンド内の先行ステップ
    （`git add` 等）の結果は反映されていない。PreToolUse hook はツール実行前に動作するため、
    この乖離を hook 側で解消することはできない（既知の制限。Issue #338）。マッチした
    `git ... commit` 区間の直前に shell 連結演算子（`&&` / `;` / `||` / `|`）があれば
    True を返す。
    """
    match = _GIT_COMMIT_PATTERN.search(command)
    if not match:
        return False
    prefix = command[: match.start()]
    return bool(_SHELL_CHAIN_OPERATOR_SUFFIX.search(prefix))


def _resolve_project_root(data: dict) -> str:
    """プロジェクトルートを解決する（無ければ空文字）。"""
    return data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")


def _codd_config_path(root: str) -> Path:
    """`.claude/config/codd/codd.yaml`（同期後の実行時 config）のパスを返す。"""
    return Path(root) / ".claude" / "config" / "codd" / "codd.yaml"


def _resolve_absolute_git_dir(root: str, env: dict[str, str]) -> str | None:
    """`root` の絶対 git-dir パスを解決する（Issue #338 反復2: drift 検査への git 履歴伝播用）。

    `git rev-parse --path-format=absolute --git-dir` は worktree 構成（`git init
    --separate-git-dir` や `git worktree add` 等）でも常に絶対パスの git-dir を返す
    （`resolve_root_worktree` と同じ resolver パターン）。解決できない場合
    （git working tree でない、subprocess の timeout / OSError 等）は None を返す。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir"],
            cwd=root,
            env=env,
            timeout=INDEX_SNAPSHOT_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    git_dir = result.stdout.strip()
    return git_dir or None


def _build_index_snapshot(root: str, env: dict[str, str]) -> tuple[str | None, str | None, str]:
    """git index の内容を一時ディレクトリへ展開する（working tree 近似の解消。Issue #338）。

    `git commit` が実際にコミットするのは working tree ではなく index の内容である。
    `git write-tree` で index の妥当性を確認したうえで、`git --work-tree=<tmp>
    checkout-index -a -f` により index の内容だけを別ディレクトリへ書き出す。実体の
    working tree・index には一切変更を加えない。あわせて `root` の絶対 git-dir も解決する
    （呼び出し元が `codd validate` サブプロセスへ `GIT_DIR`/`GIT_WORK_TREE` として渡し、
    drift 検査に実際の commit 履歴を使わせるため。反復2）。

    `env` は ambient な `GIT_DIR`/`GIT_WORK_TREE` 等を除いた環境変数
    （`hook_common.sanitized_git_env()`）。これを使わずに `os.environ` をそのまま渡すと、
    外側の実行環境（例: loop-harness の ephemeral git isolation）が設定した `GIT_DIR` が
    cwd より優先され、`root` とは無関係なリポジトリを誤って参照してしまう。

    戻り値は ``(snapshot_dir, git_dir, diagnostic)`` のタプル。構築に成功した場合は
    ``(snapshot_dir, git_dir, "")``、失敗した場合は ``(None, None, 診断メッセージ)`` を
    返す。失敗するのは主に次のケース: `root` が git working tree でない、index に
    unmerged（未解決コンフリクト）のエントリがある、絶対 git-dir を解決できない、
    subprocess の timeout / OSError。呼び出し元は成功時の一時ディレクトリを使用後に
    削除すること。
    """
    try:
        write_tree = subprocess.run(
            ["git", "write-tree"],
            cwd=root,
            env=env,
            timeout=INDEX_SNAPSHOT_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, None, f"git write-tree failed: {exc}"
    if write_tree.returncode != 0:
        reason = write_tree.stderr.strip().splitlines()[0] if write_tree.stderr.strip() else ""
        return None, None, f"git write-tree failed (code={write_tree.returncode}): {reason}"

    git_dir = _resolve_absolute_git_dir(root, env)
    if git_dir is None:
        return None, None, "git rev-parse --git-dir failed"

    snapshot_dir = tempfile.mkdtemp(prefix="codd-index-snapshot-")
    try:
        checkout = subprocess.run(
            ["git", f"--work-tree={snapshot_dir}", "checkout-index", "-a", "-f"],
            cwd=root,
            env=env,
            timeout=INDEX_SNAPSHOT_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        return None, None, f"git checkout-index failed: {exc}"
    if checkout.returncode != 0:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        reason = checkout.stderr.strip().splitlines()[0] if checkout.stderr.strip() else ""
        return None, None, f"git checkout-index failed (code={checkout.returncode}): {reason}"
    return snapshot_dir, git_dir, ""


def _run_validate(root: str) -> tuple[int, str, str] | None:
    """index スナップショットに対して `codd validate` を実行する。失敗・timeout 時は None。

    (exit_code, stdout, stderr) を返す。index スナップショットが構築できない場合、
    または `codd validate` サブプロセス自体が timeout / OSError で失敗した場合は None
    （fail-safe。呼び出し元は commit をブロックしない）。

    `codd validate` サブプロセスには `GIT_DIR`/`GIT_WORK_TREE` を明示的に渡す
    （反復2: Issue #338）。これにより codd 内部の `git status` / `git log`
    （drift 検査）は「一時ディレクトリ（index からの checkout 結果）」を working tree
    として扱いつつ、実リポジトリの commit 履歴を参照できる。
    """
    git_env = sanitized_git_env()
    snapshot_dir, git_dir, diagnostic = _build_index_snapshot(root, git_env)
    if snapshot_dir is None or git_dir is None:
        print(
            f"[codd] validate skipped: index スナップショットを構築できません（{diagnostic}）",
            file=sys.stderr,
        )
        return None

    codd_cli = os.path.join(_orchestra_dir, "packages", "codd", "scripts", "codd.py")
    validate_env = {**git_env, "GIT_DIR": git_dir, "GIT_WORK_TREE": snapshot_dir}
    try:
        result = subprocess.run(
            ["python3", codd_cli, "validate"],
            cwd=snapshot_dir,
            env=validate_env,
            timeout=VALIDATE_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[codd] validate failed: {exc}", file=sys.stderr)
        return None
    finally:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
    return result.returncode, result.stdout, result.stderr


def _extract_summary_line(stdout: str) -> str:
    """`[codd validate] errors=N warnings=M` のサマリー行を抽出する（無ければ固定文言）。"""
    for line in stdout.splitlines():
        if line.startswith("[codd validate]"):
            return line
    return "[codd validate] サマリー行を取得できませんでした"


def _emit_warn(summary: str, note: str = "") -> None:
    """warn モード: commit を通しつつ additionalContext で警告する。

    `note`（空でなければ）は複合コマンドの既知の制限注記（Issue #338）を追記する。
    """
    context = (
        f"[codd] validate でエラーを検出しました。\n{summary}\n"
        "詳細は `orchex run codd codd -- validate` で確認してください。"
    )
    if note:
        context += f"\n{note}"
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    }
    print(json.dumps(output))


def _emit_block(summary: str, note: str = "") -> None:
    """block モード: commit をブロックする（exit 2）。

    `note`（空でなければ）は複合コマンドの既知の制限注記（Issue #338）を追記する。
    """
    message = (
        f"[codd] validate でエラーを検出したため commit をブロックしました。\n{summary}\n"
        "詳細は `orchex run codd codd -- validate` で確認してください。\n"
        "この検証を緩和するには `hooks.validate_on_commit` を "
        "`warn` または `off` に変更してください（config-loading ルール参照）。"
    )
    if note:
        message += f"\n{note}"
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
    note = _COMPOUND_COMMAND_NOTE if _has_preceding_command_segment(command) else ""
    if config.hooks.validate_on_commit == cc.VALIDATE_ON_COMMIT_BLOCK:
        _emit_block(summary, note)
    else:
        _emit_warn(summary, note)
    sys.exit(0)


if __name__ == "__main__":
    main()
