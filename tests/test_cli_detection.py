"""codex exec / gemini -p の検知ロジックのテスト。

log-cli-tools.py と orchestration-route-audit.py の両方で使われている
正規表現パターンが正しく動作することを検証する。
"""

import re

import pytest

# --- 検知パターン（log-cli-tools.py / route-audit.py と同一） ---

CODEX_EXEC_RE = re.compile(
    r"(?:^|&&|\|\||;|\|)\s*"
    r"(?:timeout\s+\d+\s+)?"
    r"(?:\w+=\S+\s+)*codex\s+exec\b",
    re.IGNORECASE,
)

GEMINI_EXEC_RE = re.compile(
    r"(?:^|&&|\|\||;|\|)\s*gemini\s+-p\b",
    re.IGNORECASE,
)


# --- codex exec: 検知すべきケース ---

@pytest.mark.parametrize(
    "command",
    [
        'codex exec --model gpt-5.2-codex --sandbox read-only --full-auto "test" 2>/dev/null',
        "codex exec --model gpt-5.3-codex --sandbox workspace-write --full-auto 'hello'",
        'timeout 5 codex exec --model gpt-5.2-codex --sandbox read-only --full-auto "q"',
        'CODEX_DEBUG=1 codex exec --sandbox read-only --full-auto "debug"',
        'echo done && codex exec --full-auto "chained"',
        'true || codex exec --full-auto "fallback"',
        'pre; codex exec --full-auto "after semicolon"',
        'something | codex exec --full-auto "piped"',
    ],
    ids=[
        "basic_codex_exec",
        "workspace_write",
        "timeout_prefix",
        "env_var_prefix",
        "chained_with_and",
        "chained_with_or",
        "after_semicolon",
        "piped",
    ],
)
def test_codex_detected(command: str) -> None:
    assert CODEX_EXEC_RE.search(command), f"Should detect codex exec: {command}"


# --- codex: 検知してはいけないケース ---

@pytest.mark.parametrize(
    "command",
    [
        "ls -la ~/.codex/",
        "cat /opt/homebrew/bin/codex",
        "codex --version",
        "codex --help",
        "echo codex is great",
        "pip install codex-cli",
        "which codex",
        "file $(which codex)",
        'grep "codex" config.yaml',
    ],
    ids=[
        "ls_codex_dir",
        "cat_codex_binary",
        "codex_version",
        "codex_help",
        "echo_codex",
        "pip_install",
        "which_codex",
        "file_which_codex",
        "grep_codex",
    ],
)
def test_codex_not_detected(command: str) -> None:
    assert not CODEX_EXEC_RE.search(command), f"Should NOT detect codex exec: {command}"


# --- gemini -p: 検知すべきケース ---

@pytest.mark.parametrize(
    "command",
    [
        'gemini -p "research query" 2>/dev/null',
        "gemini -p 'What is 1+1?'",
        'gemini -p "question" --include-directories . 2>/dev/null',
        'echo done && gemini -p "chained"',
        'true; gemini -p "after semicolon"',
    ],
    ids=[
        "basic_gemini",
        "single_quote",
        "with_include_dirs",
        "chained_with_and",
        "after_semicolon",
    ],
)
def test_gemini_detected(command: str) -> None:
    assert GEMINI_EXEC_RE.search(command), f"Should detect gemini -p: {command}"


# --- gemini: 検知してはいけないケース ---

@pytest.mark.parametrize(
    "command",
    [
        "ls -la ~/.gemini/",
        "cat /opt/homebrew/bin/gemini",
        "gemini --version",
        "gemini --help",
        "echo gemini is great",
        "pip install gemini-cli",
        "which gemini",
        'grep "gemini" config.yaml',
        "gemini -v",
    ],
    ids=[
        "ls_gemini_dir",
        "cat_gemini_binary",
        "gemini_version",
        "gemini_help",
        "echo_gemini",
        "pip_install",
        "which_gemini",
        "grep_gemini",
        "gemini_other_flag",
    ],
)
def test_gemini_not_detected(command: str) -> None:
    assert not GEMINI_EXEC_RE.search(command), f"Should NOT detect gemini -p: {command}"


# --- is_codex が True のとき is_gemini は False になる ---

def test_codex_takes_priority_over_gemini() -> None:
    """codex exec コマンドに gemini が含まれていても、codex として検知される。"""
    command = 'codex exec --full-auto "compare with gemini -p approach"'
    is_codex = bool(CODEX_EXEC_RE.search(command))
    is_gemini = bool(GEMINI_EXEC_RE.search(command)) and not is_codex
    assert is_codex
    assert not is_gemini
