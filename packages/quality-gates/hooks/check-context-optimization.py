#!/usr/bin/env python3
"""
PreToolUse hook: Suggest context-saving alternatives for Read / Grep / Bash.

非効率なツール使用 (Read 全文読み・Grep content モード乱用・Bash の cat/grep 等)
を検出し、エスカレーション戦略への切り替えを提案する。

EV-21: `quality_gate.enabled=false` のときは提案を含む全動作を行わない
（`context_optimization.enabled` との AND 条件。既存の
`context_optimization.enabled` 単独での無効化は維持する）。

参照: .claude/rules/escalation-strategy.md
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from pathlib import Path

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む。
# AI_ORCHESTRA_DIR 未設定の開発・検証環境向けに、リポジトリ内の
# core/hooks へのフォールバックも用意する（Issue #134 レビュー指摘:
# post-implementation-review.py と同じフォールバック欠落。
# quality_gate_config.py と同じフォールバック方式）。
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)
else:
    _fallback_core_hooks = Path(__file__).resolve().parents[2] / "core" / "hooks"
    if str(_fallback_core_hooks) not in sys.path:
        sys.path.insert(0, str(_fallback_core_hooks))

_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

from log_common import find_project_root  # noqa: E402
from quality_gate_config import (  # noqa: E402
    load_quality_gates_config,
    resolve_quality_gate_enabled,
)

DEFAULT_READ_LINE_THRESHOLD = 200
DEFAULT_MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

# Bash で代替が望ましい先頭コマンド → 提案する代替ツール
BASH_REPLACEMENTS: dict[str, str] = {
    "cat": "Read",
    "head": "Read (offset/limit 指定)",
    "tail": "Read (末尾は offset で指定)",
    "find": "Glob",
    "grep": "Grep",
    "rg": "Grep",
}

# grep / rg で重複していた案内文言を 1 テンプレートに集約（コマンド名のみ差し替え）。
_GREP_LIKE_ADVICE_TEMPLATE = (
    "専用の Grep ツールがあればそれを使ってください。"
    "無ければ Bash の {cmd} のまま `-c`（件数）→ `-l`（ファイル名のみ）で先に絞り込み、"
    "内容表示は `| head -n N` のように上限を付けてください。"
)

BASH_SEARCH_ADVICE: dict[str, str] = {
    "grep": _GREP_LIKE_ADVICE_TEMPLATE.format(cmd="grep"),
    "rg": _GREP_LIKE_ADVICE_TEMPLATE.format(cmd="rg"),
    "find": (
        "専用の Glob ツールがあればそれを使ってください。"
        "無ければ Bash の find のまま `-maxdepth` で探索範囲を絞るか、"
        "`| head -n N` のように件数の上限を付けてください。"
    ),
}

# `rg --files`（ファイル列挙モード）専用の案内。`-c` / `-l` は検索用フラグであり、
# `--files` と併用すると `path` が検索パターンとして扱われてしまい誤動作するため、
# grep/rg の一般的な検索向け案内（BASH_SEARCH_ADVICE["rg"]）とは別に案内する。
_RG_FILES_MODE_ADVICE = (
    "`rg --files` はファイル列挙なので、専用の Glob ツールがあればそれを使い、"
    "無ければ `| head -n N` で件数に上限を付けるか `| wc -l` で件数だけ確認してください。"
)

# 検出時に剥がして次トークンを評価する単純なラッパー
BASH_WRAPPER_PREFIXES: frozenset[str] = frozenset({"sudo", "time", "nice"})

ESCALATION_REF = "参照: .claude/rules/escalation-strategy.md"

_MESSAGE_VALUE_MAX_LEN = 200


def _safe_int(value: object, default: int, *, minimum: int = 1) -> int:
    """設定値を安全に int 変換する。失敗時・下限未満時は default を返す。"""
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _sanitize_for_message(value: str, max_len: int = _MESSAGE_VALUE_MAX_LEN) -> str:
    """改行・制御文字を除去し、メッセージ埋め込み用に切り詰めた文字列を返す。"""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in value)
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3] + "..."
    return cleaned


def _load_settings(project_dir: str) -> dict:
    """quality-gates.json から context_optimization 設定を取り出す。"""
    config = load_quality_gates_config(project_dir)
    return config.get("features", {}).get("context_optimization", {}) or {}


def _load_quality_gate_settings(project_dir: str) -> dict:
    """quality-gates.json から quality_gate 設定を取り出す。"""
    config = load_quality_gates_config(project_dir)
    return config.get("features", {}).get("quality_gate", {}) or {}


def is_enabled(settings: dict) -> bool:
    """context_optimization 機能が有効かどうかを返す。デフォルト ON。"""
    return bool(settings.get("enabled", True))


def _count_lines(path: str, max_bytes: int) -> int | None:
    """通常ファイルの行数を返す。サイズ超過・特殊ファイル・I/O 失敗時は None。"""
    try:
        st = os.stat(path, follow_symlinks=True)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if st.st_size > max_bytes:
        return None
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def check_read(tool_input: dict, settings: dict) -> str:
    """Read 呼び出しを検査し、提案メッセージ (空文字なら提案なし) を返す。"""
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return ""

    has_offset = tool_input.get("offset") is not None
    has_limit = tool_input.get("limit") is not None
    if has_offset or has_limit:
        return ""

    threshold = _safe_int(
        settings.get("read_line_threshold", DEFAULT_READ_LINE_THRESHOLD),
        DEFAULT_READ_LINE_THRESHOLD,
    )
    max_bytes = _safe_int(
        settings.get("max_file_size_bytes", DEFAULT_MAX_FILE_SIZE_BYTES),
        DEFAULT_MAX_FILE_SIZE_BYTES,
    )

    line_count = _count_lines(file_path, max_bytes)
    if line_count is None or line_count <= threshold:
        return ""

    return (
        f"[Context Optimization] Read で {line_count} 行のファイルを全文読み込もうとしています。\n"
        "  → offset/limit を指定して必要範囲のみ部分読み込みを検討してください。\n"
        f"  → {ESCALATION_REF}"
    )


def check_grep(tool_input: dict, _settings: dict) -> str:
    """Grep 呼び出しを検査し、提案メッセージを返す。"""
    output_mode = tool_input.get("output_mode", "files_with_matches")
    if output_mode != "content":
        return ""
    if tool_input.get("head_limit") is not None:
        return ""

    pattern_excerpt = _sanitize_for_message(tool_input.get("pattern", ""), max_len=60)
    return (
        "[Context Optimization] Grep を content モードで head_limit 指定なしで実行しようとしています "
        f"(pattern: {pattern_excerpt!r})。\n"
        "  → まず output_mode='count' でマッチ件数を把握し、head_limit を設定してください。\n"
        f"  → {ESCALATION_REF}"
    )


def _bash_replacement(command: str) -> tuple[str, str, list[str]]:
    """command の先頭トークンを解析し、(検出されたコマンド, 推奨ツール, トークン列) を返す。

    `sudo cat foo` や `sudo nice cat foo` のような連続ラッパーは
    BASH_WRAPPER_PREFIXES に含まれる限り何段でも剥がして次のトークンを評価する。

    トークン列も返すのは、呼び出し側（`check_bash`）が検出後のコマンドに付随する
    フラグ（例: `rg --files` の `--files`）を追加で調べる際に、再度 `shlex.split`
    し直さずに済ませるため。
    """
    if not command:
        return "", "", []
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return "", "", []

    idx = 0
    while idx < len(tokens) and os.path.basename(tokens[idx]) in BASH_WRAPPER_PREFIXES:
        idx += 1
    if idx >= len(tokens):
        return "", "", tokens

    base = os.path.basename(tokens[idx])
    if base in BASH_REPLACEMENTS:
        return base, BASH_REPLACEMENTS[base], tokens
    return "", "", tokens


def check_bash(tool_input: dict, _settings: dict) -> str:
    """Bash 呼び出しを検査し、専用ツール推奨メッセージを返す。"""
    command = tool_input.get("command", "")
    used, replacement, tokens = _bash_replacement(command)
    if not used:
        return ""

    used_safe = _sanitize_for_message(used, max_len=40)
    if used == "rg" and "--files" in tokens:
        # `rg --files` はファイル列挙モードであり、検索向けの `-c`/`-l` 案内は
        # `path` を検索パターンとして扱わせてしまい誤動作を招くため、専用の
        # ファイル列挙向け案内に差し替える（BASH_SEARCH_ADVICE["rg"] は使わない）。
        advice = _RG_FILES_MODE_ADVICE
    else:
        advice = BASH_SEARCH_ADVICE.get(used)
    if advice is None:
        advice = f"代わりに {replacement} を使うと出力サイズを制御できます。"
    return (
        f"[Context Optimization] Bash で `{used_safe}` を使用しようとしています。\n"
        f"  → {advice}\n"
        f"  → {ESCALATION_REF}"
    )


CHECKERS = {
    "Read": check_read,
    "Grep": check_grep,
    "Bash": check_bash,
}


def main() -> None:
    """PreToolUse hook のエントリポイント。

    EV-10: 他の quality-gates hook と同様、main() 全体を単一の
    try/except Exception で囲み、想定外の例外（設定読み込み失敗等）でも
    stderr にログを出して exit 0 で終わる fail-open を保証する。
    """
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            sys.exit(0)

        # Issue #134 レビュー指摘: subdirectory cwd でも project root の
        # .claude/config を一貫して参照する。
        raw_project_dir = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
        project_dir = find_project_root(raw_project_dir) if raw_project_dir else find_project_root()

        # EV-21: quality_gate.enabled=false のときは提案を含む全動作を
        # 行わない（context_optimization.enabled との AND 条件）。
        quality_gate = _load_quality_gate_settings(project_dir)
        if not resolve_quality_gate_enabled(quality_gate):
            sys.exit(0)

        settings = _load_settings(project_dir)
        if not is_enabled(settings):
            sys.exit(0)

        tool_name = data.get("tool_name", "")
        checker = CHECKERS.get(tool_name)
        if checker is None:
            sys.exit(0)

        tool_input = data.get("tool_input", {}) or {}
        message = checker(tool_input, settings)

        if not message:
            sys.exit(0)

        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": message,
            }
        }
        print(json.dumps(output))
        sys.exit(0)
    except Exception as exc:
        print(f"check-context-optimization error: {exc}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
