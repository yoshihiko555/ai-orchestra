"""EV-18: exit code 規約の横断テスト。

8 hook（evaluation-set-checker.py 含む）について、post-test-analysis.py が
blocking 規約の exit code 2 を使い、他の 7 hook は非ゼロ exit を使わないことを
それぞれ独立に保証する。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_HOOKS_DIR = Path(__file__).resolve().parents[1] / "hooks"

_ALL_HOOKS = (
    "check-context-optimization.py",
    "post-implementation-review.py",
    "post-test-analysis.py",
    "lint-on-save.py",
    "test-tampering-detector.py",
    "test-gate-checker.py",
    "turn-end-summary.py",
    "evaluation-set-checker.py",
)

_BLOCKING_HOOK = "post-test-analysis.py"
_SUCCESS_EXIT_CODE = 0
_BLOCKING_EXIT_CODE = 2
_UNKNOWN_EXIT_CODE = "unknown"
_EXIT_FUNCTIONS = {("sys", "exit"), ("os", "_exit")}

type ExitCode = int | Literal["unknown"]


@dataclass(frozen=True)
class ExitCall:
    line_number: int
    exit_code: ExitCode


def _is_terminating_exit(function: ast.expr) -> bool:
    if not isinstance(function, ast.Attribute):
        return False
    if not isinstance(function.value, ast.Name):
        return False
    return (function.value.id, function.attr) in _EXIT_FUNCTIONS


def _literal_exit_code(call: ast.Call) -> ExitCode:
    if call.keywords or len(call.args) > 1:
        return _UNKNOWN_EXIT_CODE
    if not call.args:
        return _SUCCESS_EXIT_CODE

    argument = call.args[0]
    if isinstance(argument, ast.Constant) and argument.value is None:
        return _SUCCESS_EXIT_CODE
    try:
        value = ast.literal_eval(argument)
    except (TypeError, ValueError):
        return _UNKNOWN_EXIT_CODE
    return value if type(value) is int else _UNKNOWN_EXIT_CODE


def _terminating_exit_calls(source: str) -> list[ExitCall]:
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_terminating_exit(node.func)
    ]
    ordered_calls = sorted(calls, key=lambda call: (call.lineno, call.col_offset))
    return [
        ExitCall(line_number=call.lineno, exit_code=_literal_exit_code(call))
        for call in ordered_calls
    ]


def _format_exit_calls(calls: list[ExitCall]) -> str:
    return ", ".join(f"line {call.line_number}: exit code {call.exit_code}" for call in calls)


def test_extracts_every_exit_call_on_same_line() -> None:
    source = "def stop() -> None:\n    sys.exit(2); sys.exit(1)\n"

    exit_calls = _terminating_exit_calls(source)

    assert exit_calls == [
        ExitCall(line_number=2, exit_code=2),
        ExitCall(line_number=2, exit_code=1),
    ]


def test_exit_call_extraction_handles_zero_unknown_and_comments() -> None:
    source = "sys.exit()\nsys.exit(None)\nsys.exit(exit_code)\n# sys.exit(1)\n"

    assert _terminating_exit_calls(source) == [
        ExitCall(line_number=1, exit_code=0),
        ExitCall(line_number=2, exit_code=0),
        ExitCall(line_number=3, exit_code=_UNKNOWN_EXIT_CODE),
    ]


def test_post_test_analysis_uses_exit_code_2() -> None:
    hook_path = _HOOKS_DIR / _BLOCKING_HOOK
    assert hook_path.is_file(), f"{hook_path} が見つかりません"

    source = hook_path.read_text(encoding="utf-8")
    exit_calls = _terminating_exit_calls(source)
    assert any(call.exit_code == _BLOCKING_EXIT_CODE for call in exit_calls), (
        f"{_BLOCKING_HOOK} は block_on_failed_test 時に exit code 2 を使うはずですが、"
        "exit(2) の呼び出しが見つかりませんでした"
    )

    unexpected_non_zero_exits = [
        call
        for call in exit_calls
        if call.exit_code not in {_SUCCESS_EXIT_CODE, _BLOCKING_EXIT_CODE}
    ]
    assert not unexpected_non_zero_exits, (
        f"{_BLOCKING_HOOK} の非ゼロ exit は 2 のみのはずですが、"
        f"他の exit code を検出しました: {_format_exit_calls(unexpected_non_zero_exits)}"
    )


def test_other_hooks_never_use_non_zero_exit_code() -> None:
    for hook_name in _ALL_HOOKS:
        if hook_name == _BLOCKING_HOOK:
            continue

        hook_path = _HOOKS_DIR / hook_name
        assert hook_path.is_file(), f"{hook_path} が見つかりません"

        exit_calls = _terminating_exit_calls(hook_path.read_text(encoding="utf-8"))
        non_zero_exits = [call for call in exit_calls if call.exit_code != _SUCCESS_EXIT_CODE]

        assert not non_zero_exits, (
            f"{hook_name} は常に exit 0 のはずですが、非ゼロ exit を検出しました: "
            f"{_format_exit_calls(non_zero_exits)}"
        )
