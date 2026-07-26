#!/usr/bin/env python3
"""PostToolUse:Bash hook: Codex/Gemini CLI 呼び出しを検出し cli_call を記録する。"""

from __future__ import annotations

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
    # 呼び出し検出には _mask_heredoc_bodies() でマスクした文字列を使うこと
    # （prompt/model 抽出には元の command を使う。詳細は _mask_heredoc_bodies の docstring）。
    re.IGNORECASE | re.MULTILINE,
)

# heredoc ブロック全体（`<<[-]DELIM` から終端行まで）を検出する汎用パターン。
# 終端行の行頭インデント許容は heredoc 演算子の dash フラグ（group 1）で分岐する:
# - `<<-`（dash あり）: bash 仕様に合わせタブインデントを許容する
# - `<<`（dash なし。plain heredoc）: 行頭にインデントがあってはならない
#   （bash は終端行が行頭から完全一致するデリミタでない限り本文とみなす）。
#   dash なしで任意の空白を許容すると、本文中に偶然インデント済みのデリミタ単独行が
#   あった場合に本物の終端行より前で誤終端してしまう。
# `(?(1)...)` は Python re の条件付きパターン（group 1 の有無で分岐）。
HEREDOC_BLOCK_RE = re.compile(
    r"<<(-)?\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n(?(1)[\t]*|)\2[ \t]*$",
    re.DOTALL | re.MULTILINE,
)

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

    `cat > "$VAR" <<'DELIM' ... DELIM` 形式のブロックを同一コマンド文字列内から
    探し、本文を返す。ファイルシステムへのアクセスは行わない
    （呼び出し時点でファイルが既に削除・変更されていても安定して抽出できるため、
    かつ任意パス読み取りのリスクを避けるため）。

    `<<-` 形式（インデント除去付き heredoc）では、本文・終端行がタブでインデント
    されていても対応する。bash の `<<-` 仕様に合わせ、本文各行の先頭タブのみを
    除去する（スペースは除去しない）。

    終端行のインデント許容は heredoc 演算子の dash フラグ（group 1）で分岐する:
    plain heredoc（`<<`）は行頭に完全一致するデリミタのみを終端行とみなし、
    `<<-` の場合のみタブインデントを許容する（詳細は `HEREDOC_BLOCK_RE` docstring
    参照）。これにより、本文中に偶然インデント済みのデリミタ単独行があっても、
    plain heredoc では誤って途中で本文抽出が途切れない。

    トップレベル heredoc ブロックのみを対象にする（`HEREDOC_BLOCK_RE.finditer` は
    非重複マッチのため、あるブロックの本文内に別の heredoc らしき文字列が含まれて
    いても、それは外側ブロックの本文としてまとめて消費され、独立したマッチには
    ならない）。これにより、ドキュメント生成用 heredoc の本文中に同じ変数名の
    PROMPT_FILE 形式の例文が含まれていても、その例文側を実呼び出しの heredoc と
    誤認しない（`>\\s*"?\\$\\{?VAR\\}?"?\\s*` が heredoc マーカー直前に一致する
    トップレベルブロックだけを対象にするため）。単純に `pattern.search(command)`
    で raw command 全体へ leftmost search を行うと、例文側の heredoc がコマンド
    文字列中でより手前に出現する場合に誤抽出する。

    Args:
        command: Bash コマンド文字列。
        var_name: PROMPT_FILE 相当の変数名（`$` や `{}` を除いた素の名前）。

    Returns:
        heredoc 本文。検出できなければ None。
    """
    assignment_re = re.compile(r">\s*\"?\$\{?" + re.escape(var_name) + r"\}?\"?\s*$")
    for match in HEREDOC_BLOCK_RE.finditer(command):
        preceding = command[: match.start()]
        if not assignment_re.search(preceding):
            continue
        strip_leading_tabs = bool(match.group(1))
        body = match.group(3)
        if strip_leading_tabs:
            body = "\n".join(line.lstrip("\t") for line in body.split("\n"))
        content = body.strip()
        return content or None
    return None


def _mask_heredoc_bodies(command: str) -> str:
    """CLI 呼び出し検出専用に heredoc 本文をマスクした文字列を返す。

    heredoc は PROMPT_FILE への書き込みだけでなく、無関係なファイル
    （例: ドキュメント生成コマンドが `codex-delegation.md` の例文を書き込む場合）
    にも使われる。その本文中に偶然 `codex exec` のような呼び出し例が含まれると、
    `CODEX_EXEC_RE` 等の行頭アンカー検出が誤って実行呼び出しと判定してしまう。

    本文だけを空にして呼び出し検出に使うことで、この誤検知を防ぐ
    （prompt 抽出には本関数の戻り値ではなく元の command を使うこと。
    heredoc 本文そのものが抽出対象になるケースがあるため）。

    Args:
        command: Bash コマンド文字列。

    Returns:
        heredoc 本文を除去した文字列（CLI 呼び出し検出専用）。
    """

    def _blank_body(match: re.Match[str]) -> str:
        dash = match.group(1) or ""
        delimiter = match.group(2)
        return f"<<{dash}{delimiter}\n{delimiter}"

    return HEREDOC_BLOCK_RE.sub(_blank_body, command)


def extract_codex_prompt(command: str) -> str | None:
    """codex exec コマンドからプロンプトを抽出する。

    直接埋め込み形式（`codex exec ... "prompt"`）と、PROMPT_FILE 形式
    （`PROMPT_FILE=$(mktemp); cat > "$PROMPT_FILE" <<'X' ... X; codex exec ... "$(cat "$PROMPT_FILE")"`）
    の両方に対応する。

    PROMPT_FILE 形式の変数名特定は、`is_codex` 判定や `extract_model` と同様に
    heredoc 本文マスク済み文字列（`_mask_heredoc_bodies` の戻り値）に対して行う。
    生の command に対して行うと、実呼び出しより前に無関係なドキュメント生成用
    heredoc（同一変数名の PROMPT_FILE 形式の例文を含むもの）があった場合、その
    例文側を実呼び出しと誤認する（例文の heredoc 本文はマスクされて検索対象から
    除外されるため、マスク済み文字列であれば実呼び出しの変数名のみが残る）。

    Args:
        command: Bash コマンド文字列。

    Returns:
        プロンプト文字列（未切り詰め）。検出できなければ None。
        呼び出し側で `_mask_secrets` 適用後に `_truncate_prompt` で切り詰めること
        （マスク前の切り詰めはシークレットパターンの境界またぎ検知漏れを起こすため）。
    """
    detection_command = _mask_heredoc_bodies(command)
    prompt_file_match = CODEX_PROMPT_FILE_ARG_RE.search(detection_command)
    if prompt_file_match:
        # PROMPT_FILE 形式と判定した場合、legacy の引用符ベース抽出には委ねない。
        # `"$(cat "$VAR")"` は入れ子の引用符を含むため、legacy パターンに通すと
        # `$(cat` や `)` のような無意味な断片を誤抽出してしまう
        # （このメソッドが解決しようとしている元の不具合そのもの）。
        # 本文抽出自体は heredoc 本文が対象のため、マスク前の生 command に対して
        # 行う（`_extract_heredoc_content` はトップレベル heredoc ブロックのみを
        # 対象にするため、ドキュメント例文側を誤って選ばない）。
        return _extract_heredoc_content(command, prompt_file_match.group(1))

    patterns = [
        r'codex\s+exec\s+.*?--full-auto\s+"([^"]+)"',
        r"codex\s+exec\s+.*?--full-auto\s+'([^']+)'",
        r'codex\s+exec\s+.*?"([^"]+)"\s*(?:<\s*/dev/null\s*)?2>/dev/null',
        r"codex\s+exec\s+.*?'([^']+)'\s*(?:<\s*/dev/null\s*)?2>/dev/null",
    ]
    for pattern in patterns:
        match = re.search(pattern, command, re.DOTALL)
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

    呼び出し側は heredoc 本文をマスクした文字列（`_mask_heredoc_bodies` の戻り値）
    を渡すこと。生の command を渡すと、無関係な heredoc 本文（ドキュメント生成の
    例文等）に含まれる `--model` フラグを実際のモデルとして誤抽出する
    （実際の codex exec 呼び出し自体は heredoc の外側にあるため、マスクしても
    抽出結果に影響しない）。

    Args:
        command: heredoc 本文マスク済みの Bash コマンド文字列。
        tool: ツール種別（"codex" / "antigravity" / "gemini"）。

    Returns:
        モデル名。検出できなければ None。
    """
    if tool == "gemini":
        match = re.search(r"(?:^|[\s;|&])gemini\s+.*?-m\s+(\S+)", command)
        return match.group(1) if match else None
    # codex / antigravity は --model フラグを使う
    match = re.search(r"--model\s+(\S+)", command)
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

    # 呼び出し検出は heredoc 本文をマスクした文字列で行う（本文中の
    # "codex exec" 等の例文を実行呼び出しと誤検知しないため）。
    # prompt/model 抽出は元の command を使う（heredoc 本文が抽出対象のため）。
    detection_command = _mask_heredoc_bodies(command)

    is_codex = bool(CODEX_EXEC_RE.search(detection_command))
    is_antigravity = bool(ANTIGRAVITY_EXEC_RE.search(detection_command)) and not is_codex
    # gemini は移行期間中のレガシー検知（古いコマンド例・手動実行向け）
    is_gemini = bool(GEMINI_EXEC_RE.search(detection_command)) and not (is_codex or is_antigravity)

    if not (is_codex or is_antigravity or is_gemini):
        return

    # model 抽出も heredoc マスク済み文字列で行う（本文中に無関係な例文の
    # --model フラグが含まれていても、実際の呼び出しの model と誤認しないため）。
    if is_codex:
        tool = "codex"
        prompt = extract_codex_prompt(command)
        model = extract_model(detection_command) or ""
    elif is_antigravity:
        tool = "antigravity"
        prompt = extract_antigravity_prompt(command)
        model = extract_model(detection_command, tool="antigravity") or ""
    else:
        tool = "gemini"
        prompt = extract_gemini_prompt(command)
        model = extract_model(detection_command, tool="gemini") or ""

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
