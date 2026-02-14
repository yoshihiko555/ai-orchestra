#!/usr/bin/env python3
"""sub agent の JSONL 出力を人が読める形式にフォーマットする。

使い方: tail -f agent-xxx.jsonl | ./tmux-format-output.py
"""

import json
import sys

# ANSI スタイルコード
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

# アクセントカラー（重要箇所のみ使用）
YELLOW = "\033[33m"
GREEN = "\033[32m"

# 組み合わせスタイル
TOOL_NAME = f"{BOLD}{YELLOW}"  # ツール名: 太字 + 黄


def format_tool_input(input_data: dict) -> str:
    """ツール呼び出しの入力を短縮表示する。"""
    for key in ("command", "pattern", "file_path"):
        if key in input_data:
            return str(input_data[key])
    return json.dumps(input_data, ensure_ascii=False)[:120]


def handle_assistant(message: dict) -> None:
    """assistant メッセージのテキストとツール呼び出しを表示する。"""
    for content in message.get("content", []):
        content_type = content.get("type")
        if content_type == "text":
            print(content["text"])
        elif content_type == "tool_use":
            name = content.get("name", "")
            input_summary = format_tool_input(content.get("input", {}))
            print(f"{TOOL_NAME}[{name}]{RESET} {DIM}{input_summary}{RESET}")


def handle_user(message: dict) -> None:
    """ツール結果を短縮表示する。"""
    content = message.get("content")
    if not isinstance(content, list):
        return
    for item in content:
        if item.get("type") == "tool_result":
            result_text = str(item.get("content", ""))[:200]
            print(f"{DIM}  → {result_text}{RESET}")


def handle_progress(data: dict) -> None:
    """bash の進捗を表示する（出力がある場合のみ）。"""
    if data.get("type") != "bash_progress":
        return
    progress_content = data.get("content", "")
    if progress_content:
        print(f"{DIM}{str(progress_content)[:200]}{RESET}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        record_type = record.get("type", "")
        if record_type == "assistant":
            handle_assistant(record.get("message", {}))
        elif record_type == "user":
            handle_user(record.get("message", {}))
        elif record_type == "progress":
            handle_progress(record.get("data", {}))

        sys.stdout.flush()


if __name__ == "__main__":
    main()
