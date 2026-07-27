#!/usr/bin/env python3
"""PostToolUse:Bash hook: Codex/Gemini CLI 呼び出しを検出し cli_call を記録する。"""

from __future__ import annotations

import dataclasses
import os
import re
import sys

_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)
    _audit_hooks = os.path.join(_orchestra_dir, "packages", "audit", "hooks")
    if _audit_hooks not in sys.path:
        sys.path.insert(0, _audit_hooks)

from event_logger import (
    emit_event,
    load_trace_state,
    resolve_project_root_from_hook_data,
)
from hook_common import read_hook_input, safe_hook_execution
from secret_masking import mask_secrets as _mask_secrets

# ---------------------------------------------------------------------------
# CLI detection patterns
# ---------------------------------------------------------------------------

CODEX_EXEC_RE = re.compile(
    r"(?:^|&&|\|\||;|\|)\s*"
    r"(?:timeout\s+\d+\s+)?"
    r"(?:\w+=\S+\s+)*codex\s+exec\b",
    # re.MULTILINE: PROMPT_FILE 形式（heredoc でファイル書き込み後、改行を挟んで
    # `codex exec ...` を呼び出す）でも `^` が各行頭にマッチするようにする。
    # 注意: heredoc 本文中に偶然 "codex exec" 等の文字列が含まれる誤検知を防ぐため、
    # 呼び出し検出には _detection_command() でマスクした文字列を使うこと
    # （prompt/model 抽出には元の command を使う。詳細は _detection_command の docstring）。
    re.IGNORECASE | re.MULTILINE,
)

# heredoc の開始演算子（`<<[-]DELIM`）を検出するパターン。デリミタはクォート
# あり（任意の記号を含む。issue: 記号入り delimiter 対応）とクォートなし
# （シェルの区切り文字・括弧類を除く）の両方に対応する。
# `(?<!<)` / `(?!<)` は here-string（`<<<word`）を heredoc として誤認しない
# ためのガード（`<<` の直前・直後が `<` の場合は here-string の一部とみなし
# マッチさせない。issue: here-string 誤マスク対応）。
_HEREDOC_OPEN_RE = re.compile(
    r"(?<!<)<<(?!<)(-)?\s*(?:'([^'\n]*)'|\"([^\"\n]*)\"|([^\s'\"<>|&;()]+))"
)

# コマンド区切り文字（改行 / `;` / `|` / `&`）。`&&` / `||` もこの集合に含まれる
# 文字の並びとして扱われるため、区切り検出には十分。
_COMMAND_SEPARATOR_CHARS = frozenset("\n;|&")

# codex exec 呼び出しが PROMPT_FILE 経由（`"$(cat "$VAR")"`）で prompt を渡す形式
# （シェルインジェクション対策。詳細は `.claude/rules/codex-delegation.md` 参照）
CODEX_PROMPT_FILE_ARG_RE = re.compile(
    r'codex\s+exec\b.*?"\$\(cat\s+"\$\{?(\w+)\}?"\)"',
    re.DOTALL | re.IGNORECASE,
)

# テレメトリ用の prompt 上限文字数（暴走・巨大 heredoc 本文からの防御。KPI/調査用途では
# 全文である必要はなく、上限超過分は切り詰める）
MAX_PROMPT_CHARS = 20000

GEMINI_EXEC_RE = re.compile(
    r"(?:^|&&|\|\||;|\|)\s*"
    r"(?:timeout\s+\d+\s+)?"
    r"(?:\w+=\S+\s+)*gemini(?=\s|$)"
    r"(?:(?!&&|\|\||;|\|).)*\s+-p\b",
    re.IGNORECASE,
)

ANTIGRAVITY_EXEC_RE = re.compile(
    r"(?:^|&&|\|\||;|\|)\s*"
    r"(?:timeout\s+\d+\s+)?"
    r"(?:\w+=\S+\s+)*agy(?=\s|$)"
    r"(?:(?!&&|\|\||;|\|).)*\s+(?:-p|--print|--prompt)(?=\s|$)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# heredoc scanning（行単位の単一パス scanner）
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _HeredocBlock:
    """heredoc ブロック 1 個分の走査結果。"""

    start_line: int
    end_line: int
    opener_line: str
    dash: bool
    delimiter: str
    quoted: bool
    body_lines: tuple[str, ...]
    terminated: bool


def _find_heredoc_terminator(
    lines: list[str], start: int, delimiter: str, dash: bool
) -> int | None:
    """heredoc 本文の終端行を探す。

    bash 仕様では、plain heredoc（dash なし）の終端行は行全体が delimiter と
    完全一致する場合のみ認められる（末尾に空白があれば本文として扱われ、
    そこで終端しない）。`<<-` の場合のみ、先頭タブを除去した上で比較する
    （dash 形式でも末尾の空白は許容しない）。

    Args:
        lines: コマンドを改行分割した行リスト。
        start: 探索を開始する行インデックス（heredoc 本文の先頭行）。
        delimiter: 終端デリミタ文字列。
        dash: `<<-` 形式か否か。

    Returns:
        終端行のインデックス。見つからなければ None。
    """
    for index in range(start, len(lines)):
        candidate = lines[index].lstrip("\t") if dash else lines[index]
        if candidate == delimiter:
            return index
    return None


def _scan_heredocs(command: str) -> list[_HeredocBlock]:
    """command 中の heredoc ブロックを行単位の単一パスで走査する。

    正規表現の遅延 DOTALL マッチによる二次時間の悪化（終端のない heredoc
    opener が多数連結された入力で、各 opener から残り全体を再走査してしまう
    問題）を避けるため、行を 1 回だけ順に走査する。あるブロックの本文を
    消費したら、次のブロック探索はその直後の行から再開するため、同じ行を
    複数回スキャンしない（全体で O(行数) に収まる）。

    Args:
        command: Bash コマンド文字列。

    Returns:
        検出した heredoc ブロックのリスト（出現順）。
    """
    lines = command.split("\n")
    total_lines = len(lines)
    blocks: list[_HeredocBlock] = []
    index = 0
    while index < total_lines:
        open_match = _HEREDOC_OPEN_RE.search(lines[index])
        if open_match is None:
            index += 1
            continue
        dash = bool(open_match.group(1))
        # クォート済みデリミタ（`'...'` / `"..."`）か否か。unquoted delimiter
        # では bash が変数展開・command substitution を実行してから書き込む
        # ため、生の本文と実際の展開後の内容が一致しない
        # （`_extract_heredoc_content` はこのフラグを見て unquoted の場合は
        # 本文抽出を拒否し、安全側（prompt None 扱い）へ倒す）。
        quoted = open_match.group(2) is not None or open_match.group(3) is not None
        delimiter = open_match.group(2) or open_match.group(3) or open_match.group(4) or ""
        body_start = index + 1
        terminator_index = _find_heredoc_terminator(lines, body_start, delimiter, dash)
        terminated = terminator_index is not None
        body_end = terminator_index if terminated else total_lines
        blocks.append(
            _HeredocBlock(
                start_line=index,
                end_line=body_end if terminated else total_lines - 1,
                opener_line=lines[index],
                dash=dash,
                delimiter=delimiter,
                quoted=quoted,
                body_lines=tuple(lines[body_start:body_end]),
                terminated=terminated,
            )
        )
        index = (terminator_index + 1) if terminated else total_lines
    return blocks


_HEREDOC_MASK_CHAR = "#"


def _mask_heredoc_bodies(command: str) -> str:
    """CLI 呼び出し検出専用に heredoc 本文をマスクした文字列を返す。

    heredoc は PROMPT_FILE への書き込みだけでなく、無関係なファイル
    （例: ドキュメント生成コマンドが `codex-delegation.md` の例文を書き込む場合）
    にも使われる。その本文中に偶然 `codex exec` のような呼び出し例が含まれると、
    `CODEX_EXEC_RE` 等の行頭アンカー検出が誤って実行呼び出しと判定してしまう。

    本文の各行を同じ長さの placeholder 文字に置換して呼び出し検出に使うことで、
    この誤検知を防ぐ（prompt 抽出には本関数の戻り値ではなく元の command を
    使うこと。heredoc 本文そのものが抽出対象になるケースがあるため）。

    行数・各行の文字数を変更しないため、戻り値中の文字位置は元の command と
    完全に一致する（`_exec_command_segment` が検出位置をそのまま command へ
    適用できる前提になっている）。

    Args:
        command: Bash コマンド文字列。

    Returns:
        heredoc 本文をマスクした文字列（CLI 呼び出し検出専用）。
    """
    blocks = _scan_heredocs(command)
    if not blocks:
        return command
    lines = command.split("\n")
    for block in blocks:
        body_start = block.start_line + 1
        body_end = block.end_line if block.terminated else len(lines)
        for line_index in range(body_start, body_end):
            lines[line_index] = _HEREDOC_MASK_CHAR * len(lines[line_index])
    return "\n".join(lines)


def _mask_quoted_newlines(command: str) -> str:
    """引用符（`'` / `"`）内部の実改行文字を空白に置換した文字列を返す。

    `CODEX_EXEC_RE` 等は `re.MULTILINE` で `^` を使うため、引用符内に実際の
    改行が含まれていると、shell の実コマンド境界ではないのに `^` が誤って
    一致してしまう（例: `printf` に渡す複数行の quoted string の内部に、
    偶然 `codex exec ...` のような文字列が含まれる場合）。引用符内の改行だけを
    空白に置き換えることで、この誤検知を防ぐ（他の文字は変更しないため、
    文字数・文字位置は変化しない）。

    Args:
        command: Bash コマンド文字列（通常は `_mask_heredoc_bodies` 適用後）。

    Returns:
        引用符内の改行を空白に置換した文字列（元の文字列と同じ長さ）。
    """
    result_chars: list[str] = []
    quote_char: str | None = None
    escaped = False
    for char in command:
        if quote_char == "'":
            if char == "'":
                quote_char = None
            result_chars.append(" " if char == "\n" else char)
            continue
        if quote_char == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote_char = None
            result_chars.append(" " if char == "\n" else char)
            continue
        if char in ("'", '"'):
            quote_char = char
        result_chars.append(char)
    return "".join(result_chars)


def _detection_command(command: str) -> str:
    """CLI 呼び出し検出専用の文字列を返す。

    heredoc 本文のマスク（`_mask_heredoc_bodies`）と引用符内改行のマスク
    （`_mask_quoted_newlines`）を順に適用する。どちらも元の文字列と同じ
    長さ・行構造を保つため、戻り値中でマッチした位置は元の `command` に
    そのまま適用できる（`_exec_command_segment` が利用する）。

    Args:
        command: 元の Bash コマンド文字列。

    Returns:
        検出専用にマスクした文字列。
    """
    return _mask_quoted_newlines(_mask_heredoc_bodies(command))


def _find_segment_end(text: str, start: int) -> int:
    """start 以降で、引用符外にある最初のコマンド区切り文字の位置を返す。

    区切り文字（改行 / `;` / `|` / `&`）が引用符（`'` / `"`）の外側に現れた
    位置を返す。引用符の中にある区切り文字はコマンド境界とみなさない
    （prompt 文字列に `;` 等が含まれるケースを誤って区間の終端としないため）。
    見つからなければ `len(text)`（文字列末尾）を返す。

    Args:
        text: 走査対象の文字列（通常は元の Bash コマンド文字列）。
        start: 走査を開始する文字位置。

    Returns:
        区切り文字の位置、またはコマンド末尾（`len(text)`）。
    """
    quote_char: str | None = None
    escaped = False
    index = start
    length = len(text)
    while index < length:
        char = text[index]
        if quote_char == "'":
            if char == "'":
                quote_char = None
        elif quote_char == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote_char = None
        elif char in ("'", '"'):
            quote_char = char
        elif char in _COMMAND_SEPARATOR_CHARS:
            return index
        index += 1
    return length


def _last_separator_before(text: str, end: int) -> int:
    """`end` より前にある、引用符外の最後のコマンド区切り文字の直後位置を返す。

    `_find_segment_end` と逆方向（前方から `end` まで走査し、見つかった区切り
    位置を随時更新する）で同じ引用符追跡ロジックを使う。区切り文字が見つから
    なければ `0`（文字列先頭）を返す。

    Args:
        text: 走査対象の文字列（heredoc opener 行など、1行分の文字列を想定）。
        end: 走査を終了する文字位置（この位置自身は含まない）。

    Returns:
        最後に見つかった区切り文字の直後の位置。見つからなければ `0`。
    """
    quote_char: str | None = None
    escaped = False
    index = 0
    last_boundary = 0
    while index < end:
        char = text[index]
        if quote_char == "'":
            if char == "'":
                quote_char = None
        elif quote_char == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote_char = None
        elif char in ("'", '"'):
            quote_char = char
        elif char in _COMMAND_SEPARATOR_CHARS:
            last_boundary = index + 1
        index += 1
    return last_boundary


def _heredoc_producer_is_cat(opener_line: str, assignment_match: re.Match[str]) -> bool:
    """heredoc opener 行の producer コマンドが bare `cat` であるかを検証する。

    `cat > "$VAR" <<DELIM`（redirect が先）と `cat <<DELIM > "$VAR"`
    （heredoc 演算子が先）の両語順に対応する。producer が `sed` 等の変換
    コマンドの場合、実際に対象変数へ書き込まれる内容は変換後であり heredoc
    ソースの本文とは異なるため、`cat`（引数なし、標準出力をそのまま対象変数へ
    書き込む形）以外は本文抽出の対象にしない。

    Args:
        opener_line: heredoc opener 行の文字列。
        assignment_match: `>` リダイレクト（対象変数への書き込み）のマッチ。

    Returns:
        producer が引数なしの `cat` であれば True。
    """
    heredoc_match = _HEREDOC_OPEN_RE.search(opener_line)
    if heredoc_match is None:
        return False
    operator_start = min(assignment_match.start(), heredoc_match.start())
    command_start = _last_separator_before(opener_line, operator_start)
    producer = opener_line[command_start:operator_start].strip()
    return producer == "cat"


def _exec_command_segment(command: str, exec_re: re.Pattern[str]) -> str | None:
    """検出済み CLI 呼び出しのコマンド区間を返す。

    `exec_re`（例: `CODEX_EXEC_RE`）でマッチした呼び出しの開始位置から、
    引用符外にある最初のコマンド区切り（改行 / `;` / `|` / `&`）までを区間
    として切り出す。区切りが見つからなければコマンド末尾までを区間とする。

    prompt/model 抽出はこの区間内に限定して行うこと。区間外にある無関係な
    後続コマンド（例: 別の quoted な値や、後で実行される別コマンド）を
    実呼び出しの引数として誤抽出することを防ぐ。

    `_detection_command` は元の command と文字位置が完全一致するため、
    そこでマッチした位置をそのまま command 側の区間切り出しに使える。

    Args:
        command: 元の Bash コマンド文字列。
        exec_re: 検出用正規表現。

    Returns:
        コマンド区間の文字列。呼び出しが検出できなければ None。
    """
    detection_command = _detection_command(command)
    match = exec_re.search(detection_command)
    if match is None:
        return None
    segment_end = _find_segment_end(command, match.end())
    return command[match.start() : segment_end]


# ---------------------------------------------------------------------------
# Prompt / model extraction
# ---------------------------------------------------------------------------


def _truncate_prompt(prompt: str) -> str:
    """テレメトリ記録用に prompt を上限文字数へ切り詰める。

    必ず `_mask_secrets` を適用した後の文字列に対して呼び出すこと。マスク前に
    切り詰めると、ghp_/AKIA/AIza 等の固定長シークレットパターンが上限文字数の
    境界をまたぐ場合に断片化してマスク漏れを起こす（切り詰め→マスクの順序は
    危険。マスク→切り詰めの順序を守ること）。

    Args:
        prompt: マスク済みの prompt 文字列。

    Returns:
        上限以内の prompt 文字列。上限超過時は末尾に truncation マーカーを付与する。
    """
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    return prompt[:MAX_PROMPT_CHARS] + "...[truncated]"


def _extract_heredoc_content(command: str, var_name: str) -> str | None:
    """PROMPT_FILE 形式の heredoc 書き込みから本文を抽出する。

    `cat > "$VAR" <<'DELIM' ... DELIM`（および `cat <<'DELIM' > "$VAR"` の
    ようにリダイレクトと heredoc 演算子の順序が逆のケースも含む）形式の
    ブロックを同一コマンド文字列内から探し、本文を返す。ファイルシステムへの
    アクセスは行わない（呼び出し時点でファイルが既に削除・変更されていても
    安定して抽出できるため、かつ任意パス読み取りのリスクを避けるため）。

    `<<-` 形式（インデント除去付き heredoc）では、本文がタブでインデント
    されていても対応する。bash の `<<-` 仕様に合わせ、本文各行の先頭タブのみを
    除去する（スペースは除去しない）。

    末尾の改行のみを除去する（`$(cat ...)` によるコマンド置換が末尾改行だけを
    取り除く挙動に合わせるため）。本文中の先頭・末尾の水平空白（スペース）は
    意味のある prompt の一部として保持する。

    `_scan_heredocs` はトップレベル heredoc ブロックのみを検出する（あるブロック
    の本文内に別の heredoc らしき文字列が含まれていても、それは外側ブロックの
    本文としてまとめて消費され、独立したブロックにはならない）。これにより、
    ドキュメント生成用 heredoc の本文中に同じ変数名の PROMPT_FILE 形式の例文が
    含まれていても、その例文側を実呼び出しの heredoc と誤認しない
    （opener 行に `>\\s*"?\\$\\{?VAR\\}?"?` が一致するブロックだけを対象にする
    ため）。

    対象変数への書き込みに一致した最初のブロックが見つかった時点で、以下の
    2条件のいずれかを満たさない場合は安全側へ倒し `None` を返す（それ以上
    後続ブロックを探索しない。別の無関係なブロックを誤って採用するリスクを
    避けるため）:

    - `quoted`: delimiter が quoted（`<<'DELIM'` / `<<"DELIM"`）であること。
      unquoted delimiter（`<<DELIM`）は bash が変数展開・command substitution
      を書き込み前に実行するため、生の本文を記録すると実際に送信された内容と
      監査ログが乖離する（安全に展開結果を再現できないため記録自体を諦める）。
    - producer が bare `cat`（`_heredoc_producer_is_cat`）であること。`sed` 等の
      変換コマンドが producer の場合、対象変数へ実際に書き込まれる内容は
      heredoc ソースとは異なる（変換後の内容が実際に送信される）。

    Args:
        command: Bash コマンド文字列。
        var_name: PROMPT_FILE 相当の変数名（`$` や `{}` を除いた素の名前）。

    Returns:
        heredoc 本文。検出できない、または上記条件を満たさない場合は None。
    """
    assignment_re = re.compile(r'>\s*"?\$\{?' + re.escape(var_name) + r'\}?"?')
    for block in _scan_heredocs(command):
        assignment_match = assignment_re.search(block.opener_line)
        if assignment_match is None:
            continue
        if not block.quoted:
            return None
        if not _heredoc_producer_is_cat(block.opener_line, assignment_match):
            return None
        body_lines = block.body_lines
        if block.dash:
            body_lines = tuple(line.lstrip("\t") for line in body_lines)
        content = "\n".join(body_lines).rstrip("\n")
        return content or None
    return None


def extract_codex_prompt(command: str) -> str | None:
    """codex exec コマンドからプロンプトを抽出する。

    直接埋め込み形式（`codex exec ... "prompt"`）と、PROMPT_FILE 形式
    （`PROMPT_FILE=$(mktemp); cat > "$PROMPT_FILE" <<'X' ... X; codex exec ... "$(cat "$PROMPT_FILE")"`）
    の両方に対応する。

    検出済み `codex exec` 呼び出しのコマンド区間（`_exec_command_segment`）に
    限定して抽出を行う。区間を限定しない場合、同一 Bash 呼び出し内に後続の
    無関係なコマンド・quoted な値（例: 別の `"$(cat "$VAR")"`）があると、
    そちらを誤って実際のプロンプトとして抽出してしまう。

    PROMPT_FILE 形式の変数名特定はコマンド区間に対して行うが、heredoc 本文
    自体の抽出（`_extract_heredoc_content`）は元の command 全体に対して行う
    （heredoc は codex exec 呼び出しより前の行にあるため）。

    Args:
        command: Bash コマンド文字列。

    Returns:
        プロンプト文字列（未切り詰め）。検出できなければ None。
        呼び出し側で `_mask_secrets` 適用後に `_truncate_prompt` で切り詰めること
        （マスク前の切り詰めはシークレットパターンの境界またぎ検知漏れを起こすため）。
    """
    segment = _exec_command_segment(command, CODEX_EXEC_RE)
    if segment is None:
        return None

    prompt_file_match = CODEX_PROMPT_FILE_ARG_RE.search(segment)
    if prompt_file_match:
        # PROMPT_FILE 形式と判定した場合、legacy の引用符ベース抽出には委ねない。
        # `"$(cat "$VAR")"` は入れ子の引用符を含むため、legacy パターンに通すと
        # `$(cat` や `)` のような無意味な断片を誤抽出してしまう
        # （このメソッドが解決しようとしている元の不具合そのもの）。
        return _extract_heredoc_content(command, prompt_file_match.group(1))

    patterns = [
        r'codex\s+exec\s+.*?--full-auto\s+"([^"]+)"',
        r"codex\s+exec\s+.*?--full-auto\s+'([^']+)'",
        r'codex\s+exec\s+.*?"([^"]+)"\s*(?:<\s*/dev/null\s*)?2>/dev/null',
        r"codex\s+exec\s+.*?'([^']+)'\s*(?:<\s*/dev/null\s*)?2>/dev/null",
    ]
    for pattern in patterns:
        match = re.search(pattern, segment, re.DOTALL)
        if match:
            return match.group(1).strip()
    return None


def extract_gemini_prompt(command: str) -> str | None:
    """gemini コマンドからプロンプトを抽出する。

    Args:
        command: Bash コマンド文字列。

    Returns:
        プロンプト文字列。検出できなければ None。
    """
    patterns = [
        r'gemini(?=\s|$)(?:(?!&&|\|\||;|\|).)*?\s+-p\s+"([^"]+)"',
        r"gemini(?=\s|$)(?:(?!&&|\|\||;|\|).)*?\s+-p\s+'([^']+)'",
    ]
    for pattern in patterns:
        match = re.search(pattern, command, re.DOTALL)
        if match:
            return match.group(1).strip()
    return None


def extract_antigravity_prompt(command: str) -> str | None:
    """agy コマンドからプロンプトを抽出する。

    Args:
        command: Bash コマンド文字列。

    Returns:
        プロンプト文字列。検出できなければ None。
    """
    patterns = [
        r'agy(?=\s|$)(?:(?!&&|\|\||;|\|).)*?\s+(?:-p|--print|--prompt)\s+"([^"]+)"',
        r"agy(?=\s|$)(?:(?!&&|\|\||;|\|).)*?\s+(?:-p|--print|--prompt)\s+'([^']+)'",
    ]
    for pattern in patterns:
        match = re.search(pattern, command, re.DOTALL)
        if match:
            return match.group(1).strip()
    return None


def extract_model(command: str, tool: str = "codex") -> str | None:
    """コマンドからモデル名を抽出する。

    codex / antigravity では、検出済み CLI 呼び出しのコマンド区間
    （`_exec_command_segment`）に限定して `--model` フラグを検索する。区間を
    限定しない場合、無関係な heredoc 本文（ドキュメント生成の例文等）や、
    同一 Bash 呼び出し内の別コマンドに含まれる `--model` フラグを実際の
    モデルとして誤抽出してしまう。

    gemini では、heredoc 本文・引用符内改行をマスクした文字列
    （`_detection_command`）に対して検索する（gemini の抽出パターンは
    `.` が改行を跨がないため、同一行に限定される）。

    Args:
        command: 元の Bash コマンド文字列。
        tool: ツール種別（"codex" / "antigravity" / "gemini"）。

    Returns:
        モデル名。検出できなければ None。
    """
    if tool == "gemini":
        detection_command = _detection_command(command)
        match = re.search(r"(?:^|[\s;|&])gemini\s+.*?-m\s+(\S+)", detection_command)
        return match.group(1) if match else None

    exec_re = ANTIGRAVITY_EXEC_RE if tool == "antigravity" else CODEX_EXEC_RE
    segment = _exec_command_segment(command, exec_re)
    if segment is None:
        return None
    match = re.search(r"--model\s+(\S+)", segment)
    return match.group(1) if match else None


def _classify_error(exit_code: int, output: str) -> str | None:
    """エラー種別を推定する。

    Args:
        exit_code: プロセスの終了コード。
        output: プロセスの標準出力（stderr 併合含む）。

    Returns:
        エラー種別（"timeout" / "auth" / "not_found" / "rate_limit" / "unknown"）。
        exit_code が 0 の場合は None。
    """
    if exit_code == 0:
        return None
    output_lower = output.lower()
    if "timeout" in output_lower or "timed out" in output_lower:
        return "timeout"
    if "auth" in output_lower or "unauthorized" in output_lower or "403" in output_lower:
        return "auth"
    if "not found" in output_lower or "command not found" in output_lower:
        return "not_found"
    if "rate limit" in output_lower or "429" in output_lower:
        return "rate_limit"
    return "unknown"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


@safe_hook_execution
def main() -> None:
    """PostToolUse:Bash hook のエントリポイント。

    Bash コマンドから Codex/Gemini CLI 呼び出しを検出し、
    cli_call イベントを統一ログに書き出す。
    """
    data = read_hook_input()

    if data.get("tool_name") != "Bash":
        return

    tool_input = data.get("tool_input", {})
    tool_response = data.get("tool_response", {})
    command = tool_input.get("command", "")
    output = tool_response.get("stdout", "") or tool_response.get("content", "")

    # 呼び出し検出は heredoc 本文・引用符内改行をマスクした文字列で行う
    # （本文中の "codex exec" 等の例文や、quoted string 内の実改行に続く
    # 文字列を実行呼び出しと誤検知しないため）。
    detection_command = _detection_command(command)

    is_codex = bool(CODEX_EXEC_RE.search(detection_command))
    is_antigravity = bool(ANTIGRAVITY_EXEC_RE.search(detection_command)) and not is_codex
    # gemini は移行期間中のレガシー検知（古いコマンド例・手動実行向け）
    is_gemini = bool(GEMINI_EXEC_RE.search(detection_command)) and not (is_codex or is_antigravity)

    if not (is_codex or is_antigravity or is_gemini):
        return

    # prompt/model 抽出は元の command を渡す（各関数が内部で検出済み呼び出しの
    # コマンド区間に限定して抽出するため。詳細は各関数の docstring 参照）。
    if is_codex:
        tool = "codex"
        prompt = extract_codex_prompt(command)
        model = extract_model(command) or ""
    elif is_antigravity:
        tool = "antigravity"
        prompt = extract_antigravity_prompt(command)
        model = extract_model(command, tool="antigravity") or ""
    else:
        tool = "gemini"
        prompt = extract_gemini_prompt(command)
        model = extract_model(command, tool="gemini") or ""

    if not prompt:
        return

    # マスク（シークレット除去）→ 切り詰めの順序を必ず守る。切り詰め後にマスクすると
    # 固定長シークレットパターンが上限文字数の境界をまたいだ場合に検知漏れする。
    masked_prompt = _mask_secrets(prompt)
    prompt = _truncate_prompt(masked_prompt)

    # exit_code は明示的に欠落判定する（デフォルト 0 だと失敗時の success 誤判定が起きる）
    exit_code = tool_response.get("exit_code")
    success = exit_code == 0 and bool(output)
    error_type = (
        _classify_error(exit_code, output) if (not success and exit_code is not None) else None
    )

    duration_ms = tool_response.get("duration_ms")

    session_id = str(data.get("session_id", ""))
    root = resolve_project_root_from_hook_data(data)
    trace = load_trace_state(project_dir=root)
    tid = trace.get("tid", "")

    emit_event(
        "cli_call",
        {
            "tool": tool,
            "model": model,
            "prompt": prompt,
            "response": _mask_secrets(output),
            "success": success,
            "exit_code": exit_code,
            "error_type": error_type,
            "duration_ms": duration_ms,
            "retry_count": 0,
        },
        session_id=session_id,
        tid=tid,
        project_dir=root,
    )


if __name__ == "__main__":
    main()
