#!/usr/bin/env python3
"""
PreToolUse hook: Suggest context-saving alternatives for Read / Grep / Bash.

非効率なツール使用 (Read 全文読み・Grep content モード乱用・Bash の cat/grep 等)
を検出し、エスカレーション戦略への切り替えを提案する。

参照: .claude/rules/escalation-strategy.md
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

from hook_common import load_package_config  # noqa: E402

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
    """audit-flags.json から context_optimization 設定を取り出す。"""
    config = load_package_config("audit", "audit-flags.json", project_dir)
    return config.get("features", {}).get("context_optimization", {}) or {}


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


def _bash_replacement(command: str) -> tuple[str, str]:
    """command の先頭トークンを解析し、(検出されたコマンド, 推奨ツール) を返す。

    `sudo cat foo` や `sudo nice cat foo` のような連続ラッパーは
    BASH_WRAPPER_PREFIXES に含まれる限り何段でも剥がして次のトークンを評価する。
    """
    if not command:
        return "", ""
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return "", ""

    idx = 0
    while idx < len(tokens) and os.path.basename(tokens[idx]) in BASH_WRAPPER_PREFIXES:
        idx += 1
    if idx >= len(tokens):
        return "", ""

    base = os.path.basename(tokens[idx])
    if base in BASH_REPLACEMENTS:
        return base, BASH_REPLACEMENTS[base]
    return "", ""


def check_bash(tool_input: dict, _settings: dict) -> str:
    """Bash 呼び出しを検査し、専用ツール推奨メッセージを返す。"""
    command = tool_input.get("command", "")
    used, replacement = _bash_replacement(command)
    if not used:
        return ""

    used_safe = _sanitize_for_message(used, max_len=40)
    return (
        f"[Context Optimization] Bash で `{used_safe}` を使用しようとしています。\n"
        f"  → 代わりに {replacement} を使うと出力サイズを制御できます。\n"
        f"  → {ESCALATION_REF}"
    )


CHECKERS = {
    "Read": check_read,
    "Grep": check_grep,
    "Bash": check_bash,
}


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)
    if not isinstance(data, dict):
        sys.exit(0)

    project_dir = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    settings = _load_settings(project_dir)
    if not is_enabled(settings):
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    checker = CHECKERS.get(tool_name)
    if checker is None:
        sys.exit(0)

    tool_input = data.get("tool_input", {}) or {}
    try:
        message = checker(tool_input, settings)
    except Exception as exc:
        print(f"check-context-optimization error: {exc}", file=sys.stderr)
        sys.exit(0)

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


if __name__ == "__main__":
    main()
