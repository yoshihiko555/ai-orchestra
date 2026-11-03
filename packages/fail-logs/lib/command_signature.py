"""コマンドの単純コマンドへの分割・シグネチャ・意図した非ゼロ終了の判定（設計 §4 の 6・7、§7.3）。

再発サマリー（inject-failure-summary.py）と教訓化の前段判定（distill_signals.py）で共有する。
"""

from __future__ import annotations

import os
import re
import shlex

KIND_SHELL = "shell"
SHELL_SIGNATURE_TOKENS = 2
EXPECTED_NONZERO_EXIT_CODE = 1

# 引用符を解釈できないとき（閉じていない引用符）の予備の分割。
_COMMAND_SEPARATORS = re.compile(r"&&|\|\||;|\||\n")
_OPERATOR_CHARS = ";&|\n"
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def split_simple_commands(command: str) -> list[list[str]]:
    """コマンドを ``&&`` ``||`` ``;`` ``|`` と改行で単純コマンドに分ける（引用符の中では分けない）。"""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=_OPERATOR_CHARS)
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return [segment.split() for segment in _COMMAND_SEPARATORS.split(command)]
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= set(_OPERATOR_CHARS):
            segments.append([])
        else:
            segments[-1].append(token)
    return segments


def last_simple_command_tokens(command: str) -> list[str]:
    """最後の単純コマンドのトークン（先頭の環境変数の代入を除く）。"""
    segments = [segment for segment in split_simple_commands(command) if segment]
    if not segments:
        return []
    tokens = list(segments[-1])
    while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
        tokens.pop(0)
    return tokens


def is_expected_nonzero(command: str, exit_code: object, expected_commands: frozenset[str]) -> bool:
    """意図した非ゼロ終了（終了コード 1 で、最後の単純コマンドの先頭 1〜2 トークンが一致）か。"""
    if exit_code != EXPECTED_NONZERO_EXIT_CODE:
        return False
    tokens = last_simple_command_tokens(command)
    if not tokens:
        return False
    head = os.path.basename(tokens[0])
    if head in expected_commands:
        return True
    return len(tokens) >= 2 and f"{head} {tokens[1]}" in expected_commands


def signature_head(command: str, command_kind: str) -> str:
    """シグネチャの頭。shell は最後の単純コマンドの先頭 2 トークン、それ以外は先頭トークン。"""
    if command_kind == KIND_SHELL:
        return " ".join(last_simple_command_tokens(command)[:SHELL_SIGNATURE_TOKENS])
    stripped = command.strip()
    return stripped.split()[0] if stripped else ""
