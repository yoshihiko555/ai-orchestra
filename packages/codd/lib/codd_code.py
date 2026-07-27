"""コード⇔ドキュメントのトレーサビリティ抽出（Issue #98）。

静的解析でソースファイル先頭の軽量 `codd:` 注釈を読み取り、`code` / `test` kind の
CoddNode を構築する。markdown frontmatter（`codd_common.parse_codd_frontmatter`）と
同じ 1 ファイル = 1 ノード規約を踏襲しつつ、コードコメントに書きやすい 1 行形式
（`codd:<key> <value>`）を採用する。

言語別抽出方式:
- Python: `tokenize` で先頭の module docstring のみを軽量抽出する（本文コード中の
  文字列リテラルを誤って `codd:` 注釈と解釈しないため、正規表現の全文検索ではなく
  構文的に判定する。`ast.parse` によるファイル全体の構文解析は大規模コードベースで
  CPU コストが無視できないため使わない）。
- `//` 系言語（TS/JS/Go/Java/Rust/C 系等）: ファイル先頭から連続する行コメントのみを
  対象にする（shebang 行はスキップ）。

doc scope の `missing_frontmatter` 検査と異なり、注釈が無いコードファイルは黙って
スキップする（コードベース全体へのフロントマター強制はしない。opt-in な軽量記法）。
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import PurePosixPath

import codd_common as cc

# 1 行注釈の文法: `codd:<key>` または `codd:<key> <value>`。
# key が予約語（node_id/kind/status/owner）ならノード属性、それ以外は
# depends_on エントリの relation 名として扱う（value が対象 node_id）。
_ANNOTATION_LINE = re.compile(r"^codd:(?P<key>[a-z_]+)(?:\s+(?P<value>\S.*))?$")

# `codd:` で始まるが `_ANNOTATION_LINE` にマッチしない行を検出する（Issue #98 レビュー
# 対応）。ハイフン混じりの key（`codd:node-id`）や `=` 区切り（`codd:node_id=value`）等の
# 明らかなタイプミスを、黙って無視せず malformed_annotation として報告するため。
_ANNOTATION_PREFIX = re.compile(r"^codd:")

_RESERVED_KEYS = frozenset({"node_id", "kind", "status", "owner"})

# ソースファイル注釈の `codd:kind` に許される値（Issue #98 レビュー対応）。
# requirement/design 等のドキュメント語彙をソース注釈に書けてしまうと語彙が崩れるため、
# ソース由来ノードは code/test の 2 値のみを許可する。
_VALID_SOURCE_KINDS = frozenset({"code", "test"})

# 先頭 BOM（U+FEFF）。行コメント判定・Python の ast.parse は BOM 付きだと先頭行の
# 一致判定や構文解析に失敗するため、抽出前に取り除く（Issue #98 レビュー対応）。
_BOM = "﻿"

# `//` 行コメントで軽量注釈を書ける言語群（拡張子ベース）。
_LINE_COMMENT_SUFFIXES = frozenset(
    {
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".mts",
        ".cts",
        ".go",
        ".java",
        ".rs",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".kt",
        ".swift",
    }
)

# extract_code_node が対応する全拡張子（Python + `//` 系言語）。codd.py 側で読み込み
# 前に未対応拡張子を除外するために公開する（Issue #98 レビュー対応）。
SUPPORTED_SUFFIXES = _LINE_COMMENT_SUFFIXES | {".py"}

_TEST_DIR_NAMES = frozenset({"test", "tests", "__tests__"})
_TEST_NAME_PREFIXES = ("test_",)
_TEST_NAME_SUFFIXES = ("_test",)


def infer_kind(rel: str) -> str:
    """パス規約から `test` / `code` を推定する（明示 `codd:kind` が無い場合のフォールバック）。"""
    path = PurePosixPath(rel)
    if any(part.lower() in _TEST_DIR_NAMES for part in path.parts[:-1]):
        return "test"
    stem = path.stem.lower()
    if stem.startswith(_TEST_NAME_PREFIXES) or stem.endswith(_TEST_NAME_SUFFIXES):
        return "test"
    return "code"


def _slug_from_path(rel: str) -> str:
    """ファイルパスから node_id 用スラッグ（拡張子を除いたファイル名）を生成する。"""
    return PurePosixPath(rel).stem


def _parse_annotation_lines(
    block_text: str, rel: str
) -> tuple[list[tuple[str, str | None]], list[str]]:
    """行ごとに `codd:<key> <value>` 注釈を抽出する。

    `codd:` で始まりながら文法（`codd:<key>` / `codd:<key> <value>`）に一致しない行
    （例: `codd:node-id`、`codd:node_id=value`）は、単なる無関係なコメント行と区別が
    付かず黙って無視すると誤記に気付けないため、malformed_annotation エラーとして
    報告する（Issue #98 レビュー対応）。`codd:` で始まらない通常のコメント行は
    従来通り無視する。
    """
    entries: list[tuple[str, str | None]] = []
    errors: list[str] = []
    for raw_line in block_text.splitlines():
        stripped = raw_line.strip()
        match = _ANNOTATION_LINE.match(stripped)
        if match is not None:
            value = match.group("value")
            entries.append((match.group("key"), value.strip() if value else None))
            continue
        if _ANNOTATION_PREFIX.match(stripped):
            errors.append(
                f"{rel}: 不正な codd 注釈構文 '{stripped}'"
                "（'codd:<key>' または 'codd:<key> <value>' 形式で書く）"
            )
    return entries, errors


def _python_leading_text(text: str) -> str:
    """Python: モジュール docstring のみを対象領域として返す（tokenize ベース）。

    本文コード中の文字列リテラルに `codd:` らしき行があっても、docstring
    以外は見ないため誤検出しない。構文エラーの場合は空文字（注釈なし扱い）。

    `ast.parse` はファイル全体を構文解析するため、大規模コードベースでは
    CPU コストが無視できない（Issue #98 レビュー対応）。module docstring は
    「モジュール先頭の最初の文が単独の文字列リテラルであること」で決まるため、
    `tokenize` で先頭のコメント/空行トークンだけ読み飛ばし、最初の意味のある
    トークンが STRING かどうかだけを見れば十分（本文コードは走査しない。
    最初の STRING 以外のトークンに達し次第ループを抜けるため、`tokenize` の
    内部 readline も本文全体までは進まない）。抽出結果は
    `ast.get_docstring(tree, clean=False)` と同一になるよう、暗黙の文字列連結
    （``"a" "b"``）も STRING トークンが連続する間は結合し、実際の値は
    `ast.literal_eval` でデコードする。文字列以外（bytes リテラル・f-string 等）
    が最初の文だった場合は docstring 扱いしない（AST ベースと同じ挙動）。
    """
    try:
        string_tokens: list[str] = []
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING, tokenize.INDENT):
                continue
            if tok.type == tokenize.STRING:
                string_tokens.append(tok.string)
                continue
            break
        if not string_tokens:
            return ""
        value = ast.literal_eval(" ".join(string_tokens))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError, TypeError):
        return ""
    return value if isinstance(value, str) else ""


def _comment_leading_text(text: str, marker: str = "//") -> str:
    """`//` 系言語: ファイル先頭から連続する行コメントのみを対象領域として返す。

    shebang（`#!`）行はスキップする。空行に当たった時点でコメントブロック終了とみなす
    （すでに 1 行以上収集済みの場合）。
    """
    lines = text.splitlines()
    start = 1 if lines and lines[0].startswith("#!") else 0
    collected: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            if collected:
                break
            continue
        if not stripped.startswith(marker):
            break
        rest = stripped[len(marker) :]
        collected.append(rest[1:] if rest.startswith(" ") else rest)
    return "\n".join(collected)


def _entries_to_node(
    entries: list[tuple[str, str | None]], rel: str, inline_confidence: float
) -> tuple[cc.CoddNode | None, list[str]]:
    """注釈行の集合から CoddNode を組み立てる。注釈が 1 件も無ければ (None, [])。

    relation 名（予約語以外の key）に value（参照先 node_id）が無い注釈は、依存として
    黙って除外せずエラーメッセージとして返す（例: `codd:implements` のみで参照先が
    無いのは書き漏れの可能性が高く、unknown/dangling 検査で誤記を検出できないため）。
    """
    if not entries:
        return None, []
    errors = [
        f"{rel}: 注釈 'codd:{key}' に参照先 value が無い（depends_on として無効）"
        for key, value in entries
        if key not in _RESERVED_KEYS and not value
    ]
    kind_value = next((v for k, v in entries if k == "kind" and v), None)
    if kind_value is not None and kind_value not in _VALID_SOURCE_KINDS:
        # ソース注釈は code/test 語彙に限定する（Issue #98 レビュー対応）。
        # requirement/design 等のドキュメント kind を許すと語彙が崩れるため、
        # 不正値は無視して infer_kind() へフォールバックしつつエラー報告する。
        errors.append(
            f"{rel}: 注釈 'codd:kind {kind_value}' はソースファイルでは 'code' か 'test' のみ有効"
        )
        kind_value = None
    kind = kind_value or infer_kind(rel)
    node_id = (
        next((v for k, v in entries if k == "node_id" and v), None)
        or f"{kind}:{_slug_from_path(rel)}"
    )
    status = next((v for k, v in entries if k == "status" and v), "active")
    owner = next((v for k, v in entries if k == "owner" and v), None)
    deps = tuple(
        cc.Dependency(id=value, relation=key, confidence=inline_confidence)
        for key, value in entries
        if key not in _RESERVED_KEYS and value
    )
    node = cc.CoddNode(
        node_id=node_id, kind=kind, status=status, depends_on=deps, owner=owner, path=rel
    )
    return node, errors


def is_supported_suffix(rel: str) -> bool:
    """rel の拡張子が抽出対応言語（Python / `//` 系）か判定する。

    codd.py 側で読み込み前にこれを使い、code_scope の混在 glob（例: 画像とソースが
    同じディレクトリに置かれている場合）にマッチした対応外ファイルを UTF-8 テキスト
    として読み込まないようにする（Issue #98 レビュー対応）。
    """
    return PurePosixPath(rel).suffix in SUPPORTED_SUFFIXES


def extract_code_node(
    rel: str, text: str, inline_confidence: float
) -> tuple[cc.CoddNode | None, list[str]]:
    """拡張子から言語別 extractor を選び、`codd:` 注釈があれば CoddNode を返す。

    未対応拡張子、または注釈が見つからない場合は (None, [])（呼び出し側は黙ってスキップ
    する）。2 つ目の戻り値は、値の無い依存注釈（`codd:implements` のみ等）や不正な注釈
    構文を示すエラーメッセージ一覧（validate 側で `malformed_annotation` 検査として
    報告する）。

    先頭 BOM（U+FEFF）は抽出前に取り除く。BOM 付きのまま Python は ``ast.parse`` が
    構文エラーになり、`//` 系言語は先頭行のコメント判定（`startswith("//")`）に失敗する
    ため、注釈が無言でスキップされてしまう（Issue #98 レビュー対応）。
    """
    suffix = PurePosixPath(rel).suffix
    text = text.lstrip(_BOM)
    if suffix == ".py":
        leading = _python_leading_text(text)
    elif suffix in _LINE_COMMENT_SUFFIXES:
        leading = _comment_leading_text(text)
    else:
        return None, []
    entries, syntax_errors = _parse_annotation_lines(leading, rel)
    node, entry_errors = _entries_to_node(entries, rel, inline_confidence)
    return node, syntax_errors + entry_errors
