"""user_prompt_secret_scan.py のテスト。

テスト対象:
- 秘密情報パターンの検出（各種）
- 非検出時（allow）の挙動
- stdin パース失敗時の fail-open
"""

from __future__ import annotations

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
