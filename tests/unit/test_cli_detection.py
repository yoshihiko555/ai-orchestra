"""codex exec の検知ロジックのテスト。

`packages/audit/hooks/audit-cli.py` が実際に使う正規表現パターンを import して
検証する（ローカルにハードコードされたコピーは実装から drift しうるため使わない）。
"""

import pytest

from tests.module_loader import load_module

audit_cli = load_module("audit_cli", "packages/audit/hooks/audit-cli.py")

CODEX_EXEC_RE = audit_cli.CODEX_EXEC_RE


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
        'codex exec --model gpt-5.5 --sandbox read-only --full-auto "q" < /dev/null 2>/dev/null',
        "codex exec --sandbox read-only 'q' </dev/null 2>/dev/null",
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
        "stdin_redirect",
        "stdin_redirect_nospace",
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
