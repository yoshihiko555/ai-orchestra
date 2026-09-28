"""`git commit` 引数の分類（codd validate-precommit hook 用）。

`packages/codd/hooks/codd-validate-precommit.py` から切り出したモジュール（Issue #349）。
hook は `git ... commit` 呼び出しを検出したあと、この分類結果で `-a`/`--all` 候補ツリーの
再現（`codd_index_snapshot`）を行うか、再現困難として注記するかを決める。

**反復4（Issue #338、PR #339 2巡目 bot レビュー対応）**:

- **commit 引数分類の精度向上**: 値を取る短縮オプション（`-m`/`-F`/`-c`/`-C`/`-t`/`-u`）
  に到達したら、それ以降を値（attached value）として扱い走査を打ち切る（`-amfix` の
  `i` を `-i` と誤認しない、`-ma` を誤って `--all` と解釈しない）。`--pathspec-from-file`
  を再現困難モードとして分類する。

**反復6（Issue #338、PR #339 3巡目 bot レビュー追加指摘対応）**:

- **`--trailer` を値取得 long option として分類**: `classify_commit_invocation` が
  `--trailer` の値をパススペックと誤認し、`-a`/`--all` 候補ツリー再現を無効化していた
  問題を修正する。
- **後置 `--no-all` で all 判定を解除**: `--all`/`-a` の後に `--no-all` が現れた場合、
  `has_all` を `False` へ戻すようにする（git の `-a, --[no-]all` 仕様準拠）。
"""

from __future__ import annotations

import shlex

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
    "--trailer",
}
# 再現困難と明示する long option（`-p`/`-i`/`-o` の long form、および
# `--pathspec-from-file`。B-2: Issue #338 反復4 bot レビュー対応）。
_COMMIT_UNSUPPORTED_LONG_FLAGS = {
    "--patch",
    "--interactive",
    "--include",
    "--only",
    "--pathspec-from-file",
}
# 再現困難な long option のうち、値を取るもの（"=value" 形式でなければ次トークンを
# 値として読み飛ばす。B-2）。
_COMMIT_UNSUPPORTED_VALUE_LONG_FLAGS = {"--pathspec-from-file"}
# 値を取る短縮オプション文字（結合形の途中に現れたら、以降を attached value として
# 走査を打ち切る。`git commit -h` の値付きオプション: -c/-C/-F/-m/-t（必須値）/-u/-S
# （`-u[<mode>]` および `-S[<keyid>]` は attached optional value のみ。次トークンは
# 値として消費しない）。B-1: Issue #338 反復4 bot レビュー Critical 対応。`-S` は
# 反復7 bot レビュー対応で追加（`-Sabc1234` の keyid 中の `a` を `-a`/`--all` と
# 誤認しないようにするため）。
_COMMIT_VALUE_SHORT_CHARS = "cCFmtuS"
# 上記のうち、attached value が無い（トークン末尾がそのオプション文字）場合に
# 次トークンを値として読み飛ばす対象（必須値オプションのみ。`-u`/`-S` は optional
# attached value のみなので対象外 ── 次トークンを誤って消費すると `-u -m msg` の
# `-m` や `-S -m msg` の `-m` を飲み込んでしまう。`git commit -S abc` の `abc` は
# keyid ではなく pathspec 扱いになるのが git の挙動）。
_COMMIT_NEXT_TOKEN_VALUE_SHORT_CHARS = "cCFmt"
# `-a`（`--all`）に相当する短縮オプション文字。
_COMMIT_ALL_SHORT_CHAR = "a"
# 再現困難と明示する短縮オプション文字（patch / interactive / only）。
_COMMIT_UNSUPPORTED_SHORT_CHARS = "pio"
_SHELL_CHAIN_TOKENS = {"&&", "||", ";", "|"}


def classify_commit_invocation(invocation: str) -> tuple[bool, bool]:
    """`git ... commit` 呼び出しが `-a`/`--all`、および候補ツリー再現が困難なモード
    （`-p`/`--patch`/`-i`/`--interactive`/`--include`/`--only`/`--pathspec-from-file`/
    pathspec 指定）を含むかを判定する（Issue #338 反復3: bot レビュー P1 対応）。

    戻り値は ``(has_all, has_unsupported_reconstruction)``。`has_all` が True かつ
    `has_unsupported_reconstruction` が False のときのみ、呼び出し元は `-a`/`--all`
    候補ツリーの再現（`_build_commit_all_index_file`）を試みる。パース不能（引用符の
    不整合等）な場合は安全側で `(False, False)` を返す（再現は試みず、注記も付けない。
    既存の単純な index 検証にフォールバックする）。

    ``invocation`` は `git ... commit` 呼び出しの開始位置（`git`）以降の文字列。
    呼び出し元（hook）が検出用正規表現のマッチ位置で切り出して渡す（Issue #349）。

    結合形の短縮オプション（例: `-amfix`、`-ma`）は、値を取る短縮オプション文字
    （`_COMMIT_VALUE_SHORT_CHARS`）に到達した時点でそれ以降を attached value として
    扱い、フラグとしての走査を打ち切る（B-1: Issue #338 反復4 bot レビュー Critical
    対応）。これを行わないと、`-amfix` の value 部分 `"fix"` に含まれる `i` を
    `-i`（interactive）と誤認して `simulate_commit_all` を無効化したり、`-ma`
    （`-m` の attached value `"a"`）を独立した `-a` フラグと誤認して未ステージ変更を
    候補ツリーへ誤って含めてしまう（false positive）。
    """
    try:
        tokens = shlex.split(invocation)
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
        if token == "--no-all":
            has_all = False
            continue
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            if name in _COMMIT_UNSUPPORTED_LONG_FLAGS:
                has_unsupported = True
                if name in _COMMIT_UNSUPPORTED_VALUE_LONG_FLAGS and "=" not in token:
                    skip_next_value = True
                continue
            if name in _COMMIT_VALUE_LONG_FLAGS and "=" not in token:
                skip_next_value = True
            continue
        if token.startswith("-") and len(token) > 1:
            for idx, ch in enumerate(token[1:], start=1):
                if ch == _COMMIT_ALL_SHORT_CHAR:
                    has_all = True
                elif ch in _COMMIT_UNSUPPORTED_SHORT_CHARS:
                    has_unsupported = True
                if ch in _COMMIT_VALUE_SHORT_CHARS:
                    remainder = token[idx + 1 :]
                    if not remainder and ch in _COMMIT_NEXT_TOKEN_VALUE_SHORT_CHARS:
                        skip_next_value = True
                    break  # 以降は attached value。フラグとしては走査しない（B-1）。
            continue
        # commit 直後の非オプション引数 = pathspec 指定
        has_unsupported = True
    return has_all, has_unsupported
