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

**実効設定の materialize（Issue #338 反復3）**: `codd.local.yaml` は `config-loading`
ルールにより同期対象外の未追跡ファイルとして置かれる運用が通常であり、`checkout-index`
は未追跡ファイルを展開しない。そのため snapshot 上で起動される `codd validate` は
base 設定（`codd.yaml`）だけを再ロードしてしまい、local override（`scope` や
`checks.*` 等）が無視される。これを避けるため、`_run_validate` は実 root の
`.claude/config/codd/codd.yaml` と（存在すれば）`codd.local.yaml` を snapshot 側の
対応するパスへ明示的にコピーしてから `codd validate` を実行する。

**モノレポ（サブディレクトリ project root）対応（Issue #338 反復3）**: `checkout-index -a`
は index 全体（= リポジトリ全体）を snapshot_dir へ書き出すため、project root がリポジトリ
直下でない構成（例: `/repo/apps/foo`）では、`snapshot_dir` 直下ではなく
`snapshot_dir/<prefix>` に project が存在する。`git rev-parse --show-prefix` で
prefix を解決し、`codd validate` の cwd をそこに合わせる（`GIT_WORK_TREE` は
snapshot_dir のままでよい。checkout 先のパスは常に repo root 基準のため）。

**`git commit -a/--all` の候補ツリー再現（Issue #338 反復3）**: `-a`/`--all` は hook 実行
後に working tree の追跡ファイル変更を index へ取り込んでから commit するため、現在の
index をそのまま検証するだけでは実際の commit tree と一致しない。`-a`/`--all` を検出した
場合は、実 index をコピーした一時 index に対して `git add -u`（追跡済みファイルの変更・
削除を全てステージ）を適用し、その候補 index を検証する（実 index・実 working tree は
一切変更しない）。`--include`/`--only`/`-p`/`--patch`/`-i`/`--interactive`/pathspec 指定は
正確な再現が困難なため候補ツリー再現を行わず、既存の複合コマンド注記と同じ枠組みで
「この形式では hook 実行時点の index を検証しており、実際の commit tree と異なる可能性が
あります」旨を warn/block メッセージに注記する（ブロック判定自体は変えない）。

**共有 timeout budget（Issue #338 反復3）**: `write-tree` / `rev-parse` / `checkout-index` /
一時 index 構築 / `codd validate` の全 subprocess は、単一の `_Deadline`
（`HOOK_TIMEOUT_BUDGET_SECONDS`）を共有する。`packages/codd/manifest.json` の
PreToolUse timeout（90秒）より小さい値に設定し、外側の runner が hook 全体を打ち切る前に
`finally` の一時ディレクトリ削除まで確実に到達できるようにする。

**`GIT_OPTIONAL_LOCKS=0`（Issue #338 反復3）**: 実 `GIT_DIR` と一時 `GIT_WORK_TREE` の
組み合わせで `codd validate` サブプロセス内の drift 検査（`git status` / `git log`）が
走ると、Git が実 index の stat cache を refresh して書き戻すことがある（実 working
tree・index は一切変更しない設計方針に反し、`index.lock` 競合も起こしうる）。これを防ぐ
ため `codd validate` サブプロセスの env に `GIT_OPTIONAL_LOCKS=0` を渡す。

**既知の制限（Issue #338 反復3、非採用）**: 以下は実装しない（別 Issue で扱う）。
- root 内の絶対パス symlink を snapshot 側で再配置すること
- `git commit` が明示的に `GIT_INDEX_FILE` で alternate index を指定するケースへの対応
- scope 外ファイルの checkout filter 失敗による検証全体の無効化への対応
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
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

# manifest.json（packages/codd/manifest.json）の PreToolUse timeout は 90 秒。write-tree /
# rev-parse / checkout-index / 一時 index 構築（-a/--all 再現） / codd validate の全
# subprocess で単一の予算を共有し、manifest timeout 内に収める（反復3: bot レビュー P2
# 対応）。manifest 値そのものより小さく取り、hook 自身の import 等のオーバーヘッドと
# ランナー側の余裕を確保する。
HOOK_TIMEOUT_BUDGET_SECONDS = 75.0


class _Deadline:
    """全 subprocess 呼び出しで共有する残り時間予算（Issue #338 反復3）。

    個々の subprocess.run の ``timeout`` 引数には ``remaining_seconds()`` を渡す。
    予算切れ後は ``remaining_seconds()`` が 0.0 を返すため、以降の subprocess.run は
    即座に ``TimeoutExpired`` となり fail-safe に合流する。
    """

    def __init__(self, budget_seconds: float) -> None:
        self._deadline = time.monotonic() + budget_seconds

    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0


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

_UNSUPPORTED_RECONSTRUCTION_NOTE = (
    "[codd] 注記: このコマンドは `-p`/`--patch`/`-i`/`--interactive`/`--include`/`--only` "
    "またはパス指定を伴う `git commit` として検出されました。この形式では hook 実行時点の "
    "index を検証しており、実際の commit tree と異なる可能性があります"
    "（既知の制限。設計書 §4.8.1 参照）。"
)

# `git commit` の候補ツリー再現（Issue #338 反復3）で使う分類用テーブル。
# 値を取る long option（"=value" 形式でなければ次トークンを値として読み飛ばす）。
_COMMIT_VALUE_LONG_FLAGS = {
    "--message",
    "--file",
    "--author",
    "--date",
    "--template",
    "--reedit-message",
    "--reuse-message",
    "--fixup",
    "--squash",
    "--cleanup",
}
# 再現困難と明示する long option（`-p`/`-i`/`-o` の long form）。
_COMMIT_UNSUPPORTED_LONG_FLAGS = {"--patch", "--interactive", "--include", "--only"}
# 値を取る短縮オプション文字（結合形の末尾に来る前提。例: `-am` の `m`）。
_COMMIT_VALUE_SHORT_CHARS = "cCFmt"
# `-a`（`--all`）に相当する短縮オプション文字。
_COMMIT_ALL_SHORT_CHAR = "a"
# 再現困難と明示する短縮オプション文字（patch / interactive / only）。
_COMMIT_UNSUPPORTED_SHORT_CHARS = "pio"
_SHELL_CHAIN_TOKENS = {"&&", "||", ";", "|"}


def _classify_commit_invocation(command: str) -> tuple[bool, bool]:
    """`git ... commit` 呼び出しが `-a`/`--all`、および候補ツリー再現が困難なモード
    （`-p`/`--patch`/`-i`/`--interactive`/`--include`/`--only`/pathspec 指定）を
    含むかを判定する（Issue #338 反復3: bot レビュー P1 対応）。

    戻り値は ``(has_all, has_unsupported_reconstruction)``。`has_all` が True かつ
    `has_unsupported_reconstruction` が False のときのみ、呼び出し元は `-a`/`--all`
    候補ツリーの再現（`_build_commit_all_index_file`）を試みる。パース不能（引用符の
    不整合等）な場合は安全側で `(False, False)` を返す（再現は試みず、注記も付けない。
    既存の単純な index 検証にフォールバックする）。
    """
    match = _GIT_COMMIT_PATTERN.search(command)
    if not match:
        return False, False
    try:
        tokens = shlex.split(command[match.start() :])
    except ValueError:
        return False, False
    try:
        commit_at = tokens.index("commit")
    except ValueError:
        return False, False

    has_all = False
    has_unsupported = False
    seen_pathspec_separator = False
    skip_next_value = False
    for token in tokens[commit_at + 1 :]:
        if token in _SHELL_CHAIN_TOKENS:
            break
        if skip_next_value:
            skip_next_value = False
            continue
        if token == "--":
            seen_pathspec_separator = True
            continue
        if seen_pathspec_separator:
            has_unsupported = True
            continue
        if token == "--all":
            has_all = True
            continue
        if token in _COMMIT_UNSUPPORTED_LONG_FLAGS:
            has_unsupported = True
            continue
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            if name in _COMMIT_VALUE_LONG_FLAGS and "=" not in token:
                skip_next_value = True
            continue
        if token.startswith("-") and len(token) > 1:
            for ch in token[1:]:
                if ch == _COMMIT_ALL_SHORT_CHAR:
                    has_all = True
                elif ch in _COMMIT_UNSUPPORTED_SHORT_CHARS:
                    has_unsupported = True
            if token[-1] in _COMMIT_VALUE_SHORT_CHARS:
                skip_next_value = True
            continue
        # commit 直後の非オプション引数 = pathspec 指定
        has_unsupported = True
    return has_all, has_unsupported


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


def _resolve_absolute_git_dir(root: str, env: dict[str, str], deadline: _Deadline) -> str | None:
    """`root` の絶対 git-dir パスを解決する（Issue #338 反復2: drift 検査への git 履歴伝播用）。

    `git rev-parse --path-format=absolute --git-dir` は worktree 構成（`git init
    --separate-git-dir` や `git worktree add` 等）でも常に絶対パスの git-dir を返す
    （`resolve_root_worktree` と同じ resolver パターン）。解決できない場合
    （git working tree でない、subprocess の timeout / OSError 等、または `deadline`
    予算切れ）は None を返す。
    """
    if deadline.expired():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir"],
            cwd=root,
            env=env,
            timeout=deadline.remaining_seconds(),
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


def _resolve_repo_prefix(root: str, env: dict[str, str], deadline: _Deadline) -> str | None:
    """repo root から見た `root` の prefix を解決する（モノレポ対応。Issue #338 反復3）。

    `git ... checkout-index -a` は index 全体（= リポジトリ全体）を一時ディレクトリへ
    書き出すため、`root`（validate 対象のプロジェクトルート）がリポジトリ直下でない構成
    （例: `/repo/apps/foo`）では、書き出し先の中で `root` に対応する project root は
    `<snapshot_dir>/<prefix>` になる。`git rev-parse --show-prefix` はこの prefix
    （末尾 `/` 付き、repo root 自身なら空文字列）を返す。解決できない場合
    （git working tree でない、subprocess の timeout / OSError 等、または `deadline`
    予算切れ）は None を返す（呼び出し元は fail-safe としてスナップショット構築自体を
    失敗として扱う）。
    """
    if deadline.expired():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-prefix"],
            cwd=root,
            env=env,
            timeout=deadline.remaining_seconds(),
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _materialize_config(root: str, project_dir: str) -> None:
    """実 root の codd 実効設定（base + local）を snapshot 側の `project_dir` へコピーする。

    `codd.local.yaml` は `config-loading` ルールにより同期対象外の未追跡ファイルとして
    置かれる運用が通常であり、`git checkout-index` は未追跡ファイルを展開しない。その
    ため snapshot 上で起動される `codd validate` は base 設定（`codd.yaml`）だけを
    再ロードしてしまい、local override（`scope` や `checks.*` 等）が無視される
    （Issue #338 反復3: bot レビュー P1 対応）。base（`codd.yaml`、checkout-index 済みの
    内容を実 root の内容で上書きする）と local（`codd.local.yaml`、存在すれば）の両方を
    明示的にコピーすることで、snapshot 側が実 root と同じ実効設定で validate できる
    ようにする。`codd.yaml` が実 root に存在しない場合は何もしない（`main()` は既に
    exit 済みのはずだが、`_run_validate` を単体で呼ぶテストからも安全に呼べるよう
    防御的に扱う）。
    """
    config_path = _codd_config_path(root)
    if not config_path.is_file():
        return
    dest_config_path = Path(project_dir) / ".claude" / "config" / "codd" / config_path.name
    dest_config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_path, dest_config_path)

    local_path = config_path.with_name(f"{config_path.stem}.local{config_path.suffix}")
    if local_path.is_file():
        dest_local_path = dest_config_path.with_name(
            f"{dest_config_path.stem}.local{dest_config_path.suffix}"
        )
        shutil.copy2(local_path, dest_local_path)


def _build_commit_all_index_file(
    root: str, env: dict[str, str], deadline: _Deadline
) -> tuple[str | None, str]:
    """`git commit -a/--all` 相当の候補 index を一時ファイルへ構築する（Issue #338 反復3）。

    `-a`/`--all` は hook 実行後に working tree の追跡ファイル変更を index へ取り込んで
    から commit するため、現在の index をそのまま検証するだけでは実際の commit tree と
    一致しない（bot レビュー P1 対応）。実 index（`<git-dir>/index`）をコピーした一時
    index ファイルへ `GIT_INDEX_FILE` で切り替え、`git add -u`（追跡済みファイルの変更・
    削除を全てステージ。`git commit -a` と同じ意味論）を実行する。実 index・実 working
    tree は一切変更しない。

    戻り値は ``(一時 index ファイルパス, diagnostic)``。失敗時は ``(None, diagnostic)``。
    呼び出し元は成功時の一時ファイルを使用後に削除すること。
    """
    if deadline.expired():
        return None, "hook timeout budget exceeded"

    git_dir = _resolve_absolute_git_dir(root, env, deadline)
    if git_dir is None:
        return None, "git rev-parse --git-dir failed"

    real_index = Path(git_dir) / "index"
    tmp_fd, tmp_index_path = tempfile.mkstemp(prefix="codd-commit-a-index-")
    os.close(tmp_fd)
    if real_index.is_file():
        shutil.copy2(real_index, tmp_index_path)
    else:
        Path(tmp_index_path).unlink(missing_ok=True)  # 空 index: add -u が新規作成する

    add_env = {**env, "GIT_INDEX_FILE": tmp_index_path}
    try:
        add_result = subprocess.run(
            ["git", "add", "-u"],
            cwd=root,
            env=add_env,
            timeout=deadline.remaining_seconds(),
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        Path(tmp_index_path).unlink(missing_ok=True)
        return None, f"git add -u failed: {exc}"
    if add_result.returncode != 0:
        Path(tmp_index_path).unlink(missing_ok=True)
        reason = add_result.stderr.strip().splitlines()[0] if add_result.stderr.strip() else ""
        return None, f"git add -u failed (code={add_result.returncode}): {reason}"
    return tmp_index_path, ""


def _build_index_snapshot(
    root: str,
    env: dict[str, str],
    deadline: _Deadline,
    *,
    index_file: str | None = None,
) -> tuple[str | None, str | None, str | None, str]:
    """git index の内容を一時ディレクトリへ展開する（working tree 近似の解消。Issue #338）。

    `git commit` が実際にコミットするのは working tree ではなく index の内容である。
    `git write-tree` で index の妥当性を確認したうえで、`git --work-tree=<tmp>
    checkout-index -a -f` により index の内容だけを別ディレクトリへ書き出す。実体の
    working tree・index には一切変更を加えない。あわせて `root` の絶対 git-dir、および
    repo root から見た `root` の prefix（モノレポ対応。反復3）も解決する（呼び出し元が
    `codd validate` サブプロセスへ `GIT_DIR`/`GIT_WORK_TREE` として渡し、drift 検査に
    実際の commit 履歴を使わせるため。反復2）。

    `index_file` を指定すると、実 index の代わりにそのパスの index ファイルを
    `GIT_INDEX_FILE` として使う（`git commit -a/--all` 候補 index の再現用。反復3。
    `_build_commit_all_index_file` 参照）。未指定時は ambient な実 index をそのまま使う。

    `env` は ambient な `GIT_DIR`/`GIT_WORK_TREE` 等を除いた環境変数
    （`hook_common.sanitized_git_env()`）。これを使わずに `os.environ` をそのまま渡すと、
    外側の実行環境（例: loop-harness の ephemeral git isolation）が設定した `GIT_DIR` が
    cwd より優先され、`root` とは無関係なリポジトリを誤って参照してしまう。

    戻り値は ``(snapshot_dir, git_dir, prefix, diagnostic)`` のタプル。構築に成功した
    場合は ``(snapshot_dir, git_dir, prefix, "")``、失敗した場合は
    ``(None, None, None, 診断メッセージ)`` を返す。失敗するのは主に次のケース: `root`
    が git working tree でない、index に unmerged（未解決コンフリクト）のエントリが
    ある、絶対 git-dir / prefix を解決できない、subprocess の timeout / OSError、
    共有 `deadline` の予算切れ。呼び出し元は成功時の一時ディレクトリを使用後に削除
    すること。
    """
    if deadline.expired():
        return None, None, None, "hook timeout budget exceeded"

    run_env = dict(env)
    if index_file is not None:
        run_env["GIT_INDEX_FILE"] = index_file

    try:
        write_tree = subprocess.run(
            ["git", "write-tree"],
            cwd=root,
            env=run_env,
            timeout=deadline.remaining_seconds(),
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, None, None, f"git write-tree failed: {exc}"
    if write_tree.returncode != 0:
        reason = write_tree.stderr.strip().splitlines()[0] if write_tree.stderr.strip() else ""
        return None, None, None, f"git write-tree failed (code={write_tree.returncode}): {reason}"

    git_dir = _resolve_absolute_git_dir(root, env, deadline)
    if git_dir is None:
        return None, None, None, "git rev-parse --git-dir failed"

    prefix = _resolve_repo_prefix(root, env, deadline)
    if prefix is None:
        return None, None, None, "git rev-parse --show-prefix failed"

    snapshot_dir = tempfile.mkdtemp(prefix="codd-index-snapshot-")
    try:
        checkout = subprocess.run(
            ["git", f"--work-tree={snapshot_dir}", "checkout-index", "-a", "-f"],
            cwd=root,
            env=run_env,
            timeout=deadline.remaining_seconds(),
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        return None, None, None, f"git checkout-index failed: {exc}"
    if checkout.returncode != 0:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        reason = checkout.stderr.strip().splitlines()[0] if checkout.stderr.strip() else ""
        return None, None, None, f"git checkout-index failed (code={checkout.returncode}): {reason}"
    return snapshot_dir, git_dir, prefix, ""


def _run_validate(root: str, *, simulate_commit_all: bool = False) -> tuple[int, str, str] | None:
    """index スナップショットに対して `codd validate` を実行する。失敗・timeout 時は None。

    (exit_code, stdout, stderr) を返す。index スナップショットが構築できない場合、
    または `codd validate` サブプロセス自体が timeout / OSError で失敗した場合は None
    （fail-safe。呼び出し元は commit をブロックしない）。

    `simulate_commit_all=True`（`git commit -a/--all` 検出時。反復3）の場合は、実 index
    ではなく `_build_commit_all_index_file` が構築した候補 index を検証する。

    `codd validate` サブプロセスには `GIT_DIR`/`GIT_WORK_TREE` を明示的に渡す
    （反復2: Issue #338）。これにより codd 内部の `git status` / `git log`
    （drift 検査）は「一時ディレクトリ（index からの checkout 結果）」を working tree
    として扱いつつ、実リポジトリの commit 履歴を参照できる。あわせて
    `GIT_OPTIONAL_LOCKS=0` を渡し、drift 検査が実 GIT_DIR の index stat cache を
    refresh・書き戻すのを防ぐ（反復3: bot レビュー P2 対応）。

    write-tree / rev-parse / checkout-index / 一時 index 構築 / この validate 自体の
    全 subprocess は単一の `_Deadline`（`HOOK_TIMEOUT_BUDGET_SECONDS`）を共有する
    （反復3: bot レビュー P2 対応。manifest.json の PreToolUse timeout 内に収めるため）。
    """
    deadline = _Deadline(HOOK_TIMEOUT_BUDGET_SECONDS)
    git_env = sanitized_git_env()

    commit_all_index_path: str | None = None
    if simulate_commit_all:
        commit_all_index_path, diagnostic = _build_commit_all_index_file(root, git_env, deadline)
        if commit_all_index_path is None:
            print(
                f"[codd] validate skipped: -a/--all 候補 index を構築できません（{diagnostic}）",
                file=sys.stderr,
            )
            return None

    try:
        snapshot_dir, git_dir, prefix, diagnostic = _build_index_snapshot(
            root, git_env, deadline, index_file=commit_all_index_path
        )
    finally:
        if commit_all_index_path is not None:
            Path(commit_all_index_path).unlink(missing_ok=True)

    if snapshot_dir is None or git_dir is None:
        print(
            f"[codd] validate skipped: index スナップショットを構築できません（{diagnostic}）",
            file=sys.stderr,
        )
        return None

    project_dir = os.path.join(snapshot_dir, prefix) if prefix else snapshot_dir
    _materialize_config(root, project_dir)

    if deadline.expired():
        print(
            "[codd] validate skipped: hook タイムアウト予算を超過しました",
            file=sys.stderr,
        )
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        return None

    codd_cli = os.path.join(_orchestra_dir, "packages", "codd", "scripts", "codd.py")
    validate_env = {
        **git_env,
        "GIT_DIR": git_dir,
        "GIT_WORK_TREE": snapshot_dir,
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        result = subprocess.run(
            [sys.executable, codd_cli, "validate"],
            cwd=project_dir,
            env=validate_env,
            timeout=deadline.remaining_seconds(),
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

    # `-a`/`--all` は working tree の追跡ファイル変更を候補ツリーへ含める必要がある。
    # `-p`/`--patch`/`-i`/`--interactive`/`--include`/`--only`/pathspec 指定は正確な
    # 再現が困難なため、その場合は再現を試みず注記のみ付ける（反復3: Issue #338）。
    has_all, has_unsupported_reconstruction = _classify_commit_invocation(command)
    simulate_commit_all = has_all and not has_unsupported_reconstruction

    outcome = _run_validate(root, simulate_commit_all=simulate_commit_all)
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
    notes = []
    if _has_preceding_command_segment(command):
        notes.append(_COMPOUND_COMMAND_NOTE)
    if has_unsupported_reconstruction:
        notes.append(_UNSUPPORTED_RECONSTRUCTION_NOTE)
    note = "\n".join(notes)
    if config.hooks.validate_on_commit == cc.VALIDATE_ON_COMMIT_BLOCK:
        _emit_block(summary, note)
    else:
        _emit_warn(summary, note)
    sys.exit(0)


if __name__ == "__main__":
    main()
