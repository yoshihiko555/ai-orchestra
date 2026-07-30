"""EV-18: exit code 規約の横断テスト。

8 hook（evaluation-set-checker.py 含む）のうち exit code 2（ブロック）を使うのは
post-test-analysis.py のみで、他の 7 hook は常に exit 0 で終わることを保証する。
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


def test_only_post_test_analysis_uses_non_zero_exit_code() -> None:
    for hook_name in _ALL_HOOKS:
        hook_path = _HOOKS_DIR / hook_name
        assert hook_path.is_file(), f"{hook_path} が見つかりません"

        non_zero_exits = _non_zero_exit_lines(hook_path.read_text(encoding="utf-8"))

        if hook_name == _BLOCKING_HOOK:
            assert non_zero_exits, (
                f"{hook_name} は block_on_failed_test 時に exit code 2 を使うはずですが、"
                "非ゼロ exit の呼び出しが見つかりませんでした"
            )
        else:
            assert not non_zero_exits, (
                f"{hook_name} は常に exit 0 のはずですが、非ゼロ exit を検出しました: "
                f"{non_zero_exits}"
            )
