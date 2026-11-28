"""サマリーと教訓化で共有する分割・判定を検証する（EV-33・EV-34）。"""

from __future__ import annotations

import sys

import pytest

from tests.module_loader import REPO_ROOT, load_module

for _path in (
    REPO_ROOT / "packages" / "core" / "hooks",
    REPO_ROOT / "packages" / "fail-logs" / "lib",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import command_signature

inject = load_module(
    "inject_failure_summary_signature", "packages/fail-logs/hooks/inject-failure-summary.py"
)
EXPECTED_COMMANDS = frozenset(inject.DEFAULT_SUMMARY["expected_nonzero_commands"])


def test_last_simple_command_tokens() -> None:
    assert command_signature.last_simple_command_tokens("a && b c") == ["b", "c"]
    assert command_signature.last_simple_command_tokens("a || b | c d ; e f") == ["e", "f"]
    assert command_signature.last_simple_command_tokens("FOO=1 BAR=2 grep x") == ["grep", "x"]
    assert command_signature.last_simple_command_tokens("") == []


# EV-33 / EV-34: 引用符の外の演算子だけで分け、最後の単純コマンドを採用する。
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("a && b", ["b"]),
        ("x; y", ["y"]),
        ("a|b", ["b"]),
        ("a & b", ["b"]),
        ("a |& b", ["b"]),
        ("cmd\nnext arg", ["next", "arg"]),
        ("grep -F '|' file.txt", ["grep", "-F", "|", "file.txt"]),
        ('"grep" x', ["grep", "x"]),
        ("echo 5 > f", ["echo", "5"]),
    ],
)
def test_last_simple_command_tokens_preserve_shell_words(command: str, expected: list[str]) -> None:
    assert command_signature.last_simple_command_tokens(command) == expected


# EV-34: リダイレクトの演算子と宛先は、コマンドのトークンに含めない。
@pytest.mark.parametrize(
    "redirect",
    [
        "2>&1",
        ">&2",
        "&>/dev/null",
        "&>>file",
        "2>file",
        "2>>file",
        ">file",
        "<file",
        "<<EOF",
        "<&3",
        ">|file",
        "<<<value",
        "<>file",
        "> 'file with spaces'",
    ],
)
def test_redirections_are_dropped_from_command_tokens(redirect: str) -> None:
    assert command_signature.last_simple_command_tokens(f"make build {redirect}") == [
        "make",
        "build",
    ]
    assert command_signature.last_simple_command_tokens(f"pytest {redirect}") == ["pytest"]


# EV-33: 引用符やエスケープが閉じていない入力でも、予備の分割で処理を続ける。
@pytest.mark.parametrize("command", ['grep "unterminated', "grep trailing" + "\\"])
def test_unparseable_shell_input_falls_back(command: str) -> None:
    assert command_signature.last_simple_command_tokens(command) == command.split()


def test_summary_uses_shared_expected_nonzero_classifier() -> None:
    assert inject.is_expected_nonzero is command_signature.is_expected_nonzero


@pytest.mark.parametrize(
    "command",
    [
        "grep foo file 2>&1",
        "grep foo file > out.txt",
        "cat a.log | grep foo",
        "make build && grep foo file",
        "make build; grep foo file",
    ],
)
def test_expected_nonzero_decisions_agree(command: str) -> None:
    data = {"command": command, "detected_by": "exit_code", "exit_code": 1}

    assert inject._is_expected_nonzero(data, EXPECTED_COMMANDS)
    assert command_signature.is_expected_nonzero(command, 1, EXPECTED_COMMANDS)


@pytest.mark.parametrize("redirect", ["2>&1", "> out.txt"])
def test_redirected_grep_keeps_its_command_tokens(redirect: str) -> None:
    assert command_signature.split_simple_commands(f"grep foo file {redirect}") == [
        ["grep", "foo", "file"]
    ]


def test_trailing_build_is_not_expected_nonzero() -> None:
    command = "grep foo file 2>&1 && make build"
    data = {"command": command, "detected_by": "exit_code", "exit_code": 1}

    assert command_signature.last_simple_command_tokens(command) == ["make", "build"]
    assert not inject._is_expected_nonzero(data, EXPECTED_COMMANDS)
    assert not command_signature.is_expected_nonzero(command, 1, EXPECTED_COMMANDS)


def test_quoted_semicolon_is_a_shell_word() -> None:
    assert command_signature.last_simple_command_tokens('grep -E "a;b" x') == [
        "grep",
        "-E",
        "a;b",
        "x",
    ]


def test_heredoc_and_output_targets_are_not_command_tokens() -> None:
    assert command_signature.last_simple_command_tokens("cat > run.sh <<EOF") == ["cat"]
