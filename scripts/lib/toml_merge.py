"""TOML ドキュメントの部分マージヘルパー（セクション単位 / キー単位）。

正規表現ベースの行走査で対象範囲だけを書き換え、コメントや既存の行順序を
できる限り保持する（全体再シリアライズはしない）。
packages/cocoindex/hooks/provision-mcp-servers.py の `_find_toml_section()` と
同じ走査方式を、特定パッケージに依存しない汎用ユーティリティとして独立させたもの
（cocoindex 側の実装はそのまま、こちらは新規の共通ヘルパー）。

マージ結果は書き込み前に必ず tomllib.loads() で妥当性検証する（fail-closed）。
"""

from __future__ import annotations

import re
import tomllib

# Section header must be the *entire* (stripped) line: "[name]" optionally
# followed by a trailing comment. The name itself may not contain brackets,
# which naturally excludes "[[array-of-tables]]" headers (unsupported by
# this simple merge helper) as well as array-literal continuation lines
# like "[1, 2]," that appear inside multi-line arrays.
_TOML_HEADER_RE = re.compile(r"^\[([^\[\]]+)\]\s*(?:#.*)?$")

# Line endings that indicate the *next* line is still inside a multi-line
# construct (an array literal or a line-continued value) and therefore must
# never be treated as a section header, even if it happens to look like one
# (e.g. a bare "[3, 4]" as the last element of an array-of-arrays).
_CONTINUATION_SUFFIXES = ("[", ",", "\\")

# TOML multi-line string delimiters. A line that (re)opens one of these
# puts every following line into "inside a string" mode until a line
# containing the same delimiter closes it again -- during which time a
# bracketed line (e.g. a "[section]"-looking line quoted inside the string
# body) must never be mistaken for a section header.
_TRIPLE_QUOTE_DELIMS = ('"""', "'''")


def _is_toml_header_line(stripped: str) -> re.Match[str] | None:
    """Match `stripped` against the section-header pattern.

    Lines starting with ``[[`` (array-of-tables) never match, since the
    header pattern's character class excludes nested ``[``.
    """
    return _TOML_HEADER_RE.match(stripped)


def _compute_skip_lines(lines: list[str]) -> list[bool]:
    """Return, per line, whether it must never be treated as a header candidate.

    A line is skipped when it is still part of a multi-line construct
    opened on an earlier line: either an array-literal/line-continuation
    (``_CONTINUATION_SUFFIXES``) or a ``\"\"\"``/``'''`` multi-line string
    literal. This is a line-oriented heuristic (not a full TOML tokenizer),
    matching the rest of this module's approach; the result is always
    re-validated via ``tomllib.loads()`` before being written (fail-closed).
    """
    skip = [False] * len(lines)
    in_continuation = False
    in_triple_string: str | None = None

    for i, line in enumerate(lines):
        if in_triple_string is not None:
            skip[i] = True
            if line.count(in_triple_string) % 2 == 1:
                in_triple_string = None
            continue

        skip[i] = in_continuation

        stripped = line.strip()
        in_continuation = bool(stripped) and stripped.endswith(_CONTINUATION_SUFFIXES)

        for delim in _TRIPLE_QUOTE_DELIMS:
            if line.count(delim) % 2 == 1:
                in_triple_string = delim
                # A newly opened multi-line string always resets any
                # continuation state: even if this line's `endswith()` check
                # above happened to match one of `_CONTINUATION_SUFFIXES`
                # (e.g. the string body itself ends with "["), that state
                # must not be carried across the string body and mistakenly
                # applied to the real header line right after it closes.
                in_continuation = False
                break

    return skip


class TomlMergeError(ValueError):
    """マージ結果が妥当な TOML にならない場合に送出する例外。"""


def _validate_toml(content: str) -> None:
    """マージ後のコンテンツを tomllib でパース検証する（fail-closed）。"""
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as e:
        msg = f"invalid TOML after merge: {e}"
        raise TomlMergeError(msg) from e


def find_toml_section(content: str, section_name: str) -> tuple[int, int] | None:
    """指定セクションの開始行・終了行（排他）を行番号で返す。

    次のセクションヘッダまたは EOF を終端とする。見つからなければ None。
    """
    lines = content.splitlines()
    skip = _compute_skip_lines(lines)
    start: int | None = None
    for i, line in enumerate(lines):
        if skip[i]:
            continue
        stripped = line.strip()
        match = _is_toml_header_line(stripped)
        if match is None:
            continue
        header = match.group(1)
        if start is not None:
            return (start, i)
        if header == section_name:
            start = i
    if start is not None:
        return (start, len(lines))
    return None


def upsert_toml_section(content: str, section_name: str, new_section: str) -> str:
    """指定セクションを置換、無ければ末尾に追記する。

    Args:
        content: 既存の TOML 文字列。
        section_name: セクション名（`[` `]` を含まない。例: "mcp_servers.foo"）。
        new_section: ヘッダ行を含む完全なセクション文字列
            （例: "[mcp_servers.foo]\\ncommand = \\"...\\""）。

    Returns:
        マージ後の TOML 文字列。既存セクションと内容が同一なら変更なしで返す。

    Raises:
        TomlMergeError: マージ結果が妥当な TOML にならない場合。
    """
    span = find_toml_section(content, section_name)

    if span is None:
        merged = _append_section(content, new_section)
        _validate_toml(merged)
        return merged

    lines = content.splitlines()
    old_section = "\n".join(lines[span[0] : span[1]])
    if old_section.rstrip() == new_section.rstrip():
        return content

    merged_lines = lines[: span[0]] + new_section.splitlines() + lines[span[1] :]
    merged = "\n".join(merged_lines) + "\n"
    _validate_toml(merged)
    return merged


def _append_section(content: str, new_section: str) -> str:
    """コンテンツ末尾に新規セクションを追記する。"""
    if not content.strip():
        return new_section.rstrip() + "\n"
    separator = "\n" if content.endswith("\n") else "\n\n"
    return content + separator + new_section.rstrip() + "\n"


def upsert_toml_key_in_section(
    content: str,
    section_name: str,
    key: str,
    value: str,
    *,
    overwrite: bool = True,
) -> str:
    """`[section_name]` テーブル内の `key = value` 行を追加/更新する。

    セクション自体が存在しない場合は、そのセクションを新規追記する。

    Args:
        content: 既存の TOML 文字列。
        section_name: 対象セクション名（`[` `]` を含まない）。
        key: 追加/更新するキー名。
        value: TOML 値として展開済みの文字列（例: "true", '"foo"', "1"）。
        overwrite: False の場合、既存キーがあれば変更せず content をそのまま返す。

    Returns:
        マージ後の TOML 文字列。

    Raises:
        TomlMergeError: マージ結果が妥当な TOML にならない場合。
    """
    span = find_toml_section(content, section_name)
    if span is None:
        new_section = f"[{section_name}]\n{key} = {value}"
        return upsert_toml_section(content, section_name, new_section)

    lines = content.splitlines()
    start, end = span
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    existing_line = _find_key_line(lines, start + 1, end, key_re)

    if existing_line is None:
        new_lines = lines[: start + 1] + [f"{key} = {value}"] + lines[start + 1 :]
        merged = "\n".join(new_lines) + "\n"
        _validate_toml(merged)
        return merged

    return _apply_key_update(lines, existing_line, key, value, overwrite)


def upsert_toml_top_level_key(
    content: str,
    key: str,
    value: str,
    *,
    overwrite: bool = True,
) -> str:
    """先頭セクションヘッダより前のトップレベルスカラーキーを追加/更新する。

    セクションヘッダが一つも無い場合はファイル全体をトップレベル領域とみなす。

    Args:
        content: 既存の TOML 文字列。
        key: 追加/更新するキー名（例: "default_permissions"）。
        value: TOML 値として展開済みの文字列。
        overwrite: False の場合、既存キーがあれば変更せず content をそのまま返す。

    Returns:
        マージ後の TOML 文字列。

    Raises:
        TomlMergeError: マージ結果が妥当な TOML にならない場合。
    """
    lines = content.splitlines()
    top_end = _find_first_header_index(lines)
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    existing_line = _find_key_line(lines, 0, top_end, key_re)

    if existing_line is None:
        new_lines = lines[:top_end] + [f"{key} = {value}"] + lines[top_end:]
        merged = "\n".join(new_lines) + "\n"
        _validate_toml(merged)
        return merged

    return _apply_key_update(lines, existing_line, key, value, overwrite)


def _apply_key_update(
    lines: list[str],
    line_index: int,
    key: str,
    value: str,
    overwrite: bool,
) -> str:
    """既存キー行を更新して妥当性検証済みの TOML 文字列を返す（変更なしなら元行のまま）。"""
    if _line_value(lines[line_index]) == value.strip():
        return "\n".join(lines) + "\n"
    if not overwrite:
        return "\n".join(lines) + "\n"

    lines[line_index] = f"{key} = {value}"
    merged = "\n".join(lines) + "\n"
    _validate_toml(merged)
    return merged


def _find_key_line(lines: list[str], start: int, end: int, key_re: re.Pattern[str]) -> int | None:
    """[start, end) 範囲で key_re にマッチする最初の行番号を返す。"""
    for i in range(start, end):
        if key_re.match(lines[i]):
            return i
    return None


def _line_value(line: str) -> str:
    """`key = value` 形式の行から value 部分（前後空白を除く）を取り出す。"""
    _, _, rest = line.partition("=")
    return rest.strip()


def _find_first_header_index(lines: list[str]) -> int:
    """最初のセクションヘッダ行番号を返す。無ければ len(lines) を返す。

    複数行配列（継続コンテキスト）や複数行文字列リテラル内の行は
    ヘッダ候補として扱わない。
    """
    skip = _compute_skip_lines(lines)
    for i, line in enumerate(lines):
        if skip[i]:
            continue
        if _is_toml_header_line(line.strip()):
            return i
    return len(lines)
