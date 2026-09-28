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

**Issue #349（allowlist 化）**: 未知のオプションを個別に無視・パススルーする方式は、
表の登録漏れや将来の git のオプション追加で、値中の文字を `-a` と誤読するリスクを
常に抱える（`-amfix` / `-ma` / `-S<keyid>` で実際に発生した）。本モジュールは
commit tree に影響しないと分かっているオプションだけを allowlist として通し、
それ以外（未知のオプション・解析できないコマンド）は「再現困難」として安全側に倒す
（EV-99 / EV-100）。トークナイズは `shlex.shlex` の `punctuation_chars` を使い、
shell 演算子・改行を空白なしでも区切りとして扱う（EV-101）。
"""

from __future__ import annotations

import re
import shlex

# `git commit` の後続引数トークナイズで区切りとして扱う shell 演算子・改行文字
# （EV-101）。`shlex.shlex(punctuation_chars=...)` に渡すと、これらの文字は
# 空白が無くても独立したトークンとして切り出される（例: `x&&echo` -> ['x', '&&', 'echo']）。
_SHELL_PUNCTUATION_CHARS = "();<>|&\n"
# bash の行継続（`\` + 改行）。区切りではないため分割前に取り除く（EV-101）。
_LINE_CONTINUATION = re.compile(r"(?<!\\)\\\n")
# 空白に続く数字だけの fd 指定（`2>&1` の `2`）。リダイレクトの一部であり引数ではないため、
# 分割前に取り除いてリダイレクト演算子として扱わせる（EV-101）。
_FD_REDIRECT_PREFIX = re.compile(r"(?<=\s)\d+(?=[<>])")

# ---------------------------------------------------------------------------
# 短縮オプション文字の分類テーブル（allowlist）。各集合は互いに素（テストで担保）。
# ---------------------------------------------------------------------------

# 値を必須で取る短縮オプション（attached value が無ければ次トークンを値として消費）。
# `-m`/`-F`/`-c`/`-C`/`-t`。
_SHORT_VALUE_REQUIRED_CHARS = frozenset("mFcCt")
# attached value のみを取る短縮オプション（次トークンは値として消費しない）。
# `-u`（`--untracked-files`）/`-S`（`--gpg-sign`）。
_SHORT_VALUE_OPTIONAL_CHARS = frozenset("uS")
# `-a`（`--all`）に相当する短縮オプション文字。
_SHORT_ALL_CHAR = "a"
# 値を取らず commit tree に影響しない短縮オプション（中立）。
_SHORT_NEUTRAL_CHARS = frozenset("enqsv")
# 再現困難と明示する短縮オプション（patch / interactive / only）。
_SHORT_UNSUPPORTED_CHARS = frozenset("pio")

# ---------------------------------------------------------------------------
# long option の分類テーブル（allowlist）。各集合は互いに素（テストで担保）。
# ---------------------------------------------------------------------------

# 値を取る long option（"=value" 形式でなければ次トークンを値として読み飛ばす）。
_LONG_VALUE_FLAGS = frozenset(
    {
        "--message",
        "--file",
        "--author",
        "--date",
        "--template",
        "--reuse-message",
        "--reedit-message",
        "--fixup",
        "--squash",
        "--cleanup",
        "--trailer",
    }
)
# 値を取らず commit tree に影響しない long option（中立）。
_LONG_NO_VALUE_FLAGS = frozenset(
    {
        "--amend",
        "--edit",
        "--no-edit",
        "--verify",
        "--no-verify",
        "--signoff",
        "--no-signoff",
        "--quiet",
        "--verbose",
        "--allow-empty",
        "--allow-empty-message",
        "--reset-author",
        "--no-gpg-sign",
    }
)
# `=value` 形式のときだけ値を取る long option（bare 形は値を取らない。次トークンは
# 消費しない）。`--gpg-sign keyid` の `keyid` はこのオプションの値ではなく、別の
# 非オプショントークン（pathspec）として扱われる。
_LONG_VALUE_ONLY_VIA_EQUALS_FLAGS = frozenset({"--gpg-sign", "--untracked-files"})
# 再現困難と明示する long option（値を取らないもの）。
_LONG_UNSUPPORTED_NO_VALUE_FLAGS = frozenset({"--patch", "--interactive", "--include", "--only"})
# 再現困難と明示する long option のうち、値を取るもの（"=value" 形式でなければ
# 次トークンを値として読み飛ばす）。
_LONG_UNSUPPORTED_VALUE_FLAGS = frozenset({"--pathspec-from-file"})


def classify_commit_invocation(invocation: str) -> tuple[bool, bool]:
    """`git ... commit` 呼び出しが `-a`/`--all`、および候補ツリー再現が困難なモードを
    含むかを allowlist 方式で判定する（Issue #349）。

    戻り値は ``(has_all, has_unsupported_reconstruction)``。`has_all` が True かつ
    `has_unsupported_reconstruction` が False のときのみ、呼び出し元は `-a`/`--all`
    候補ツリーの再現（`_build_commit_all_index_file`）を試みる。

    commit tree に影響しないと分かっているオプション（`_SHORT_*`/`_LONG_*` の
    allowlist）だけを解釈し、それ以外の未知のオプション・pathspec 指定・パース不能な
    コマンドはすべて `has_unsupported_reconstruction=True` に倒す（EV-99）。読み違いが
    起きても「候補ツリーを再現せず注記が付く」安全側にのみ倒れる。

    パース不能（引用符の不整合等）、または `commit` トークンを特定できない場合は
    `(False, True)` を返す（EV-100。黙って通常の index 検証にフォールバックするのでは
    なく、再現困難として注記する）。`git commit` に続く引数が無い場合は
    `(False, False)`。

    ``invocation`` は `git ... commit` 呼び出しの開始位置（`git`）以降の文字列。
    呼び出し元（hook）が検出用正規表現のマッチ位置で切り出して渡す（Issue #349）。
    サブコマンドの位置は最初の `commit` トークンとみなす。`git -C commit commit -a` のように
    グローバルオプションの値が `commit` だと位置を読み違えるが、本来のサブコマンドが
    pathspec 扱いになり `has_unsupported=True` の安全側に倒れる。

    トークナイズは `shlex.shlex` に `punctuation_chars=_SHELL_PUNCTUATION_CHARS` を
    渡し、shell 演算子（`&&`/`||`/`;`/`|`/`&`/括弧/リダイレクト）と改行を空白が
    無くても区切りとして扱う（EV-101。例: `git commit -a -m x&&echo y` の `&&` 以降は
    走査しない）。
    """
    source = _FD_REDIRECT_PREFIX.sub("", _LINE_CONTINUATION.sub("", invocation))
    try:
        lexer = shlex.shlex(source, posix=True, punctuation_chars=_SHELL_PUNCTUATION_CHARS)
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        # shlex の既定は語中の `#` 以降もコメントとして捨てる（bash は語頭の `#` だけ）。
        # 捨てると後続の `-a` / `--no-all` を見落とすため無効にし、`#` を含む語は通常の語
        # として扱う（語頭の `#` で始まるコメントは pathspec 扱いになり安全側に倒れる）。
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False, True

    try:
        commit_at = tokens.index("commit")
    except ValueError:
        return False, True

    has_all = False
    has_unsupported = False
    seen_pathspec_separator = False
    skip_next_value = False

    for token in tokens[commit_at + 1 :]:
        # 値待ちを先に消費する。引用符で囲んだ値（`-m ';'`）は shlex 上で演算子と区別できない
        # ため、演算子判定を先にすると以降のオプション（`--only` 等）を見落とす。未クォートの
        # 演算子を値として飲み込んでも、後続の語は pathspec になり安全側に倒れる。
        if skip_next_value:
            skip_next_value = False
            continue
        # shell 演算子・改行トークン（punctuation_chars のみで構成される）の扱い（EV-101）。
        # リダイレクト（`<` / `>` を含む）はリダイレクト先の 1 語だけを読み飛ばして走査を
        # 続ける（`git commit -m x 2>&1 -a` の `-a` は git に渡る）。制御演算子（`;` / `&&` /
        # `||` / `&` / `|` / 括弧 / 改行）に到達したら走査を打ち切る。
        if token and set(token) <= set(_SHELL_PUNCTUATION_CHARS):
            if "<" in token or ">" in token:
                skip_next_value = True
                continue
            break
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
            name, has_equals = token.split("=", 1)[0], "=" in token
            if name in _LONG_UNSUPPORTED_NO_VALUE_FLAGS:
                has_unsupported = True
                continue
            if name in _LONG_UNSUPPORTED_VALUE_FLAGS:
                has_unsupported = True
                if not has_equals:
                    skip_next_value = True
                continue
            if name in _LONG_VALUE_FLAGS:
                if not has_equals:
                    skip_next_value = True
                continue
            if name in _LONG_NO_VALUE_FLAGS:
                continue
            if name in _LONG_VALUE_ONLY_VIA_EQUALS_FLAGS:
                # bare form（"=" 無し）は値を取らない。次トークンは消費しない ──
                # 続く非オプショントークンは下の「非オプション = pathspec」に落ちる。
                continue
            # 未知の long option。allowlist に無いため再現困難として注記する（EV-99）。
            has_unsupported = True
            continue
        if token.startswith("-") and len(token) > 1:
            for idx, ch in enumerate(token[1:], start=1):
                if ch == _SHORT_ALL_CHAR:
                    has_all = True
                    continue
                if ch in _SHORT_NEUTRAL_CHARS:
                    continue
                if ch in _SHORT_UNSUPPORTED_CHARS:
                    has_unsupported = True
                    continue
                if ch in _SHORT_VALUE_REQUIRED_CHARS or ch in _SHORT_VALUE_OPTIONAL_CHARS:
                    remainder = token[idx + 1 :]
                    if not remainder and ch in _SHORT_VALUE_REQUIRED_CHARS:
                        skip_next_value = True
                    break  # 以降は attached value。フラグとしては走査しない。
                # 未知の短縮オプション文字。allowlist に無いため再現困難として注記し、
                # 残りは値の可能性があるため走査を打ち切る（`-Kab` の `a` を `-a` と
                # 誤認しない）。
                has_unsupported = True
                break
            continue
        # commit 直後の非オプション引数 = pathspec 指定
        has_unsupported = True
    return has_all, has_unsupported
