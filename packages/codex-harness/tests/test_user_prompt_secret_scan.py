"""user_prompt_secret_scan.py のテスト。

テスト対象:
- 秘密情報パターンの検出（各種）
- 非検出時（allow）の挙動
- stdin パース失敗時の fail-open
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.module_loader import load_module

secret_scan = load_module(
    "user_prompt_secret_scan",
    "packages/codex-harness/codex/hooks/user_prompt_secret_scan.py",
)


class TestFindMatches:
    def test_detects_openai_api_key_assignment(self) -> None:
        matches = secret_scan.find_matches("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz")
        assert "OPENAI_API_KEY assignment" in matches

    def test_detects_aws_access_key_id(self) -> None:
        matches = secret_scan.find_matches("please use AWS_ACCESS_KEY_ID=AKIA...")
        assert "AWS_ACCESS_KEY_ID" in matches

    def test_detects_aws_secret_access_key(self) -> None:
        matches = secret_scan.find_matches("AWS_SECRET_ACCESS_KEY=abc123")
        assert "AWS_SECRET_ACCESS_KEY" in matches

    def test_detects_github_token_keyword(self) -> None:
        matches = secret_scan.find_matches("export GITHUB_TOKEN=xxxx")
        assert "GITHUB_TOKEN" in matches

    def test_detects_ghp_prefixed_token(self) -> None:
        matches = secret_scan.find_matches("token: ghp_abcdefghijklmnopqrstuvwxyz1234")
        assert "GitHub PAT (ghp_)" in matches

    def test_detects_github_pat_prefixed_token(self) -> None:
        matches = secret_scan.find_matches("github_pat_abcdefghijklmnopqrstuvwxyz1234")
        assert "GitHub fine-grained PAT (github_pat_)" in matches

    def test_detects_sk_prefixed_key(self) -> None:
        matches = secret_scan.find_matches("key is sk-abcdefghij1234")
        assert "API key (sk- prefix)" in matches

    def test_detects_pem_private_key_block(self) -> None:
        matches = secret_scan.find_matches("-----BEGIN RSA PRIVATE KEY-----\nMIIB...")
        assert "PEM private key block" in matches

    def test_returns_empty_for_clean_prompt(self) -> None:
        matches = secret_scan.find_matches("Please implement the login form component.")
        assert matches == []

    def test_short_sk_like_string_is_not_flagged(self) -> None:
        matches = secret_scan.find_matches("sk-1")
        assert matches == []

    def test_detects_lowercase_openai_api_key_assignment(self) -> None:
        """R14: keyword patterns must be case-insensitive (e.g. lowercase env var names)."""
        matches = secret_scan.find_matches("openai_api_key=sk-abcdefghijklmnopqrstuvwxyz")
        assert "OPENAI_API_KEY assignment" in matches

    def test_detects_lowercase_aws_access_key_id(self) -> None:
        matches = secret_scan.find_matches("please use aws_access_key_id=AKIA...")
        assert "AWS_ACCESS_KEY_ID" in matches

    def test_detects_lowercase_aws_secret_access_key(self) -> None:
        matches = secret_scan.find_matches("aws_secret_access_key=abc123")
        assert "AWS_SECRET_ACCESS_KEY" in matches

    def test_detects_lowercase_github_token_keyword(self) -> None:
        matches = secret_scan.find_matches("export github_token=xxxx")
        assert "GITHUB_TOKEN" in matches


class TestReadStdinPayload:
    def test_returns_none_on_invalid_json(self, monkeypatch) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert secret_scan.read_stdin_payload() is None

    def test_returns_none_for_non_object_json(self, monkeypatch) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("[1, 2, 3]"))
        assert secret_scan.read_stdin_payload() is None

    def test_parses_valid_payload(self, monkeypatch) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO('{"prompt": "hello"}'))
        payload = secret_scan.read_stdin_payload()
        assert payload == {"prompt": "hello"}


class TestMain:
    def test_exits_zero_when_stdin_is_invalid(self, monkeypatch) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert secret_scan.main() == 0

    def test_exits_zero_for_clean_prompt(self, monkeypatch) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO('{"prompt": "add a feature"}'))
        assert secret_scan.main() == 0

    def test_exits_two_when_secret_detected(self, monkeypatch, capsys) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO('{"prompt": "OPENAI_API_KEY=abc"}'))
        assert secret_scan.main() == 2
        captured = capsys.readouterr()
        assert "OPENAI_API_KEY assignment" in captured.err
        assert "abc" not in captured.err


class TestReexecUnderTargetInterpreter:
    """_reexec_under_target_interpreter(): AI_ORCHESTRA_PYTHON によるインタプリタ切替（Issue #345）。

    Codex CLI は ``.codex/hooks.json`` の ``"command": "python3 <path>"`` をそのまま起動する
    ため、``python3`` の解決先は hook 自身の制御外にある。``AI_ORCHESTRA_PYTHON`` が設定されて
    いる場合のみ re-exec するオプトイン仕様であり、未設定時は既存の挙動を一切変えない。
    """

    def test_noop_when_env_var_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("AI_ORCHESTRA_PYTHON", raising=False)
        monkeypatch.delenv("_AI_ORCHESTRA_HOOK_REEXECED", raising=False)
        calls: list = []
        monkeypatch.setattr(secret_scan.os, "execv", lambda *a: calls.append(a))

        secret_scan._reexec_under_target_interpreter()

        assert calls == []

    def test_noop_when_target_matches_current_interpreter(self, monkeypatch) -> None:
        monkeypatch.setenv("AI_ORCHESTRA_PYTHON", secret_scan.sys.executable)
        monkeypatch.delenv("_AI_ORCHESTRA_HOOK_REEXECED", raising=False)
        calls: list = []
        monkeypatch.setattr(secret_scan.os, "execv", lambda *a: calls.append(a))

        secret_scan._reexec_under_target_interpreter()

        assert calls == []

    def test_noop_when_target_missing(self, tmp_path, monkeypatch) -> None:
        missing = tmp_path / "no-such-python3"
        monkeypatch.setenv("AI_ORCHESTRA_PYTHON", str(missing))
        monkeypatch.delenv("_AI_ORCHESTRA_HOOK_REEXECED", raising=False)
        calls: list = []
        monkeypatch.setattr(secret_scan.os, "execv", lambda *a: calls.append(a))

        secret_scan._reexec_under_target_interpreter()

        assert calls == []

    def test_noop_when_sentinel_already_set(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "fake-python3"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_PYTHON", str(target))
        monkeypatch.setenv("_AI_ORCHESTRA_HOOK_REEXECED", "1")
        calls: list = []
        monkeypatch.setattr(secret_scan.os, "execv", lambda *a: calls.append(a))

        secret_scan._reexec_under_target_interpreter()

        assert calls == []

    def test_execs_target_when_different_interpreter(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "fake-python3"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_PYTHON", str(target))
        monkeypatch.delenv("_AI_ORCHESTRA_HOOK_REEXECED", raising=False)
        calls: list = []
        monkeypatch.setattr(secret_scan.os, "execv", lambda *a: calls.append(a))

        secret_scan._reexec_under_target_interpreter()

        assert calls == [(str(target), [str(target), *secret_scan.sys.argv])]
        assert secret_scan.os.environ["_AI_ORCHESTRA_HOOK_REEXECED"] == "1"


class TestReexecUnderTargetInterpreterSubprocess:
    """実際にサブプロセスとして起動し、re-exec が発生することを黒箱で検証する（Issue #345）。

    上の TestReexecUnderTargetInterpreter は `_reexec_under_target_interpreter()` を関数
    単体でテストしており `os.execv` はモック済みで実行されない。この 1 ケースだけは、
    `python3 <hook>` として実際に子プロセスを起動し、`AI_ORCHESTRA_PYTHON` に設定した
    偽インタプリタへ `os.execv` が本当に切り替わることを stdout のマーカーで確認する
    （advisor が挙げた「fake interpreter script that prints a marker」パターン）。
    """

    def test_reexecs_under_ai_orchestra_python_when_set(self, tmp_path: Path) -> None:
        shim = tmp_path / "fake-python3"
        shim.write_text('#!/bin/sh\necho "REEXEC_MARKER:$@"\n', encoding="utf-8")
        shim.chmod(0o755)

        hook_path = Path(secret_scan.__file__)
        env = {**os.environ, "AI_ORCHESTRA_PYTHON": str(shim)}
        env.pop("_AI_ORCHESTRA_HOOK_REEXECED", None)

        result = subprocess.run(
            [sys.executable, str(hook_path)],
            input="{}",
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
            check=False,
        )

        assert f"REEXEC_MARKER:{hook_path}" in result.stdout
