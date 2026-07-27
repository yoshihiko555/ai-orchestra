"""コード⇔ドキュメントのトレーサビリティ抽出（Issue #98）。

静的解析でソースファイル先頭の軽量 `codd:` 注釈を読み取り、`code` / `test` kind の
CoddNode を構築する。markdown frontmatter（`codd_common.parse_codd_frontmatter`）と
同じ 1 ファイル = 1 ノード規約を踏襲しつつ、コードコメントに書きやすい 1 行形式
（`codd:<key> <value>`）を採用する。

言語別抽出方式:
- Python: `ast` でモジュール docstring のみを対象にする（本文コード中の文字列リテラルを
  誤って `codd:` 注釈と解釈しないため、正規表現の全文検索ではなく AST を使う）。
- `//` 系言語（TS/JS/Go/Java/Rust/C 系等）: ファイル先頭から連続する行コメントのみを
  対象にする（shebang 行はスキップ）。

doc scope の `missing_frontmatter` 検査と異なり、注釈が無いコードファイルは黙って
スキップする（コードベース全体へのフロントマター強制はしない。opt-in な軽量記法）。
"""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

import codd_common as cc

# 1 行注釈の文法: `codd:<key>` または `codd:<key> <value>`。
# key が予約語（node_id/kind/status/owner）ならノード属性、それ以外は
# depends_on エントリの relation 名として扱う（value が対象 node_id）。
_ANNOTATION_LINE = re.compile(r"^codd:(?P<key>[a-z_]+)(?:\s+(?P<value>\S.*))?$")

_RESERVED_KEYS = frozenset({"node_id", "kind", "status", "owner"})

# `//` 行コメントで軽量注釈を書ける言語群（拡張子ベース）。
_LINE_COMMENT_SUFFIXES = frozenset(
    {
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
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


def _parse_annotation_lines(block_text: str) -> list[tuple[str, str | None]]:
    """行ごとに `codd:<key> <value>` 注釈を抽出する（マッチしない行は無視）。"""
    entries: list[tuple[str, str | None]] = []
    for raw_line in block_text.splitlines():
        match = _ANNOTATION_LINE.match(raw_line.strip())
        if match is None:
            continue
        value = match.group("value")
        entries.append((match.group("key"), value.strip() if value else None))
    return entries


def _python_leading_text(text: str) -> str:
    """Python: モジュール docstring のみを対象領域として返す（AST ベース）。

    本文コード中の文字列リテラルに `codd:` らしき行があっても、docstring
    以外は見ないため誤検出しない。構文エラーの場合は空文字（注釈なし扱い）。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ""
    return ast.get_docstring(tree, clean=False) or ""


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
    kind = next((v for k, v in entries if k == "kind" and v), None) or infer_kind(rel)
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


def extract_code_node(
    rel: str, text: str, inline_confidence: float
) -> tuple[cc.CoddNode | None, list[str]]:
    """拡張子から言語別 extractor を選び、`codd:` 注釈があれば CoddNode を返す。

    未対応拡張子、または注釈が見つからない場合は (None, [])（呼び出し側は黙ってスキップ
    する）。2 つ目の戻り値は、値の無い依存注釈（`codd:implements` のみ等）を示す
    エラーメッセージ一覧（validate 側で `malformed_annotation` 検査として報告する）。
    """
    suffix = PurePosixPath(rel).suffix
    if suffix == ".py":
        leading = _python_leading_text(text)
    elif suffix in _LINE_COMMENT_SUFFIXES:
        leading = _comment_leading_text(text)
    else:
        return None, []
    entries = _parse_annotation_lines(leading)
    return _entries_to_node(entries, rel, inline_confidence)
