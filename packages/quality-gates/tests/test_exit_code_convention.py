"""EV-18: exit code 規約の横断テスト。

8 hook（evaluation-set-checker.py 含む）について、post-test-analysis.py が
blocking 規約の exit code 2 を使い、他の 7 hook は非ゼロ exit を使わないことを
それぞれ独立に保証する。
"""

from __future__ import annotations

import re
from pathlib import Path

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

# sys.exit(N) や os._exit(N) の N!=0 呼び出しを検出する（コメントアウト行は除外）。
_NON_ZERO_EXIT_PATTERN = re.compile(r"^\s*(?:sys\.exit|os\._exit)\(\s*(?!0\s*\))\d+")


def _non_zero_exit_lines(source: str) -> list[str]:
    return [line for line in source.splitlines() if _NON_ZERO_EXIT_PATTERN.match(line)]


def test_post_test_analysis_uses_exit_code_2() -> None:
    hook_path = _HOOKS_DIR / _BLOCKING_HOOK
    assert hook_path.is_file(), f"{hook_path} が見つかりません"

    source = hook_path.read_text(encoding="utf-8")
    exit_code_2_pattern = re.compile(r"^\s*(?:sys\.exit|os\._exit)\(\s*2\s*\)", re.MULTILINE)
    assert exit_code_2_pattern.search(source), (
        f"{_BLOCKING_HOOK} は block_on_failed_test 時に exit code 2 を使うはずですが、"
        "exit(2) の呼び出しが見つかりませんでした"
    )

    unexpected_non_zero_exits = [
        line for line in _non_zero_exit_lines(source) if not exit_code_2_pattern.match(line)
    ]
    assert not unexpected_non_zero_exits, (
        f"{_BLOCKING_HOOK} の非ゼロ exit は 2 のみのはずですが、"
        f"他の exit code を検出しました: {unexpected_non_zero_exits}"
    )


def test_other_hooks_never_use_non_zero_exit_code() -> None:
    for hook_name in _ALL_HOOKS:
        if hook_name == _BLOCKING_HOOK:
            continue

        hook_path = _HOOKS_DIR / hook_name
        assert hook_path.is_file(), f"{hook_path} が見つかりません"

        non_zero_exits = _non_zero_exit_lines(hook_path.read_text(encoding="utf-8"))

        assert not non_zero_exits, (
            f"{hook_name} は常に exit 0 のはずですが、非ゼロ exit を検出しました: {non_zero_exits}"
        )
