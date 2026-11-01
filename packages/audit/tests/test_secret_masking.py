"""secret_masking.py の共通マスキングロジックのテスト。"""

from __future__ import annotations

import sys
import time

import pytest

from tests.module_loader import REPO_ROOT, load_module

_audit_hooks = str(REPO_ROOT / "packages" / "audit" / "hooks")
_core_hooks = str(REPO_ROOT / "packages" / "core" / "hooks")
_routing_hooks = str(REPO_ROOT / "packages" / "agent-routing" / "hooks")
for p in [_audit_hooks, _core_hooks, _routing_hooks]:
    if p not in sys.path:
        sys.path.insert(0, p)

secret_masking = load_module("secret_masking", "packages/audit/hooks/secret_masking.py")
audit_cli = load_module("audit_cli", "packages/audit/hooks/audit-cli.py")
audit_prompt = load_module("audit_prompt", "packages/audit/hooks/audit-prompt.py")


class TestMaskSecrets:
    """`mask_secrets` のテスト（共通モジュール本体）。"""

    def test_masks_openai_key(self) -> None:
        text = "here is sk-abcdefghijklmnopqrstuvwxyz012345"
        assert "[REDACTED]" in secret_masking.mask_secrets(text)

    def test_masks_azure_sas_token(self) -> None:
        """Azure SAS トークン（SharedAccessSignature=）がマスクされることを確認する。"""
        text = "conn=https://x.blob.core.windows.net/c?SharedAccessSignature=sv=2020&sig=abc123"
        result = secret_masking.mask_secrets(text)
        assert "SharedAccessSignature" not in result or "[REDACTED]" in result
        assert "sig=abc123" not in result

    def test_masks_aws_access_key_id(self) -> None:
        key_id = "AK" + "IA" + "IOSFODNN7EXAMPLE"
        masked = secret_masking.mask_secrets(f"id: {key_id} end")
        assert key_id not in masked
        assert "[REDACTED]" in masked
        assert "end" in masked

    def test_masks_google_api_key(self) -> None:
        api_key = "AI" + "za" + "Sy" + "A" * 33
        masked = secret_masking.mask_secrets(f"id: {api_key} end")
        assert api_key not in masked
        assert "[REDACTED]" in masked
        assert "end" in masked

    def test_masks_pem_private_key_block(self) -> None:
        """PEM 秘密鍵ブロック全体（BEGIN・本文・END）がマスクされることを確認する。"""
        rsa_begin = "-----BEGIN RSA " + "PRIVATE " + "KEY-----"
        rsa_end = "-----END RSA " + "PRIVATE " + "KEY-----"
        body_line1 = "MIIBogIBAAJ..."
        body_line2 = "c29tZS1mYWtlLWJhc2U2NC1ib2R5LWxpbmU="
        text = f"{rsa_begin}\n{body_line1}\n{body_line2}\n{rsa_end}"
        result = secret_masking.mask_secrets(text)
        assert rsa_begin not in result
        assert body_line1 not in result
        assert body_line2 not in result
        assert rsa_end not in result
        assert "[REDACTED]" in result

    def test_masks_plain_pem_block(self) -> None:
        """RSA/OPENSSH 等の接頭辞なし PEM 秘密鍵も本文・END 込みでマスクされることを確認する。"""
        begin = "-----BEGIN " + "PRIVATE " + "KEY-----"
        end = "-----END " + "PRIVATE " + "KEY-----"
        body_line1 = "MIIBogIBAAJ..."
        body_line2 = "c29tZS1mYWtlLWJhc2U2NC1ib2R5LWxpbmU="
        text = f"{begin}\n{body_line1}\n{body_line2}\n{end}"
        result = secret_masking.mask_secrets(text)
        assert begin not in result
        assert body_line1 not in result
        assert body_line2 not in result
        assert end not in result
        assert "[REDACTED]" in result

    def test_masks_bearer_token(self) -> None:
        auth_scheme = "Bear" + "er"
        text = f"Authorization: {auth_scheme} abc123.def456-ghi"
        assert "[REDACTED]" in secret_masking.mask_secrets(text)

    def test_non_secret_text_unchanged(self) -> None:
        text = "hello world, this has no secrets"
        assert secret_masking.mask_secrets(text) == text

    def test_masks_quoted_multi_word_value(self) -> None:
        """クォート付き・空白を含む複数語の値が最初の空白で途切れず全体マスクされることを確認する。"""
        text = 'password: "correct horse battery staple" end'
        result = secret_masking.mask_secrets(text)
        assert "correct horse battery staple" not in result
        assert "[REDACTED]" in result
        assert "end" in result

    def test_masks_single_quoted_multi_word_value(self) -> None:
        text = "token='my very long api token value' trailing"
        result = secret_masking.mask_secrets(text)
        assert "my very long api token value" not in result
        assert "trailing" in result

    def test_masks_double_quoted_value_with_escaped_quote(self) -> None:
        text = 'password="first\\" second"'
        result = secret_masking.mask_secrets(text)
        assert "first" not in result
        assert "second" not in result
        assert '\\"' not in result
        assert "[REDACTED]" in result

    def test_masks_single_quoted_value_with_escaped_quote(self) -> None:
        text = "token='first\\' second'"
        result = secret_masking.mask_secrets(text)
        assert "first" not in result
        assert "second" not in result
        assert "\\'" not in result
        assert "[REDACTED]" in result

    def test_masks_unquoted_value_until_comma(self) -> None:
        """クォートなしの値もカンマ/セミコロン/改行まで丸ごとマスクされることを確認する。"""
        text = "secret=abc def ghi, next_field=1"
        result = secret_masking.mask_secrets(text)
        assert "abc def ghi" not in result
        assert "next_field=1" in result

    def test_masks_connection_string(self) -> None:
        """`scheme://user:pass@host` 形式の接続文字列がマスクされることを確認する。"""
        text = "DATABASE_URL=postgres://dbuser:sup3rSecr3t@db.example.com:5432/prod"
        result = secret_masking.mask_secrets(text)
        assert "dbuser" not in result
        assert "sup3rSecr3t" not in result
        assert "[REDACTED]" in result

    def test_does_not_mask_plain_url_without_credentials(self) -> None:
        """認証情報を含まない通常の URL はマスク対象外であることを確認する。"""
        text = "See https://example.com/docs for details"
        assert secret_masking.mask_secrets(text) == text

    def test_masks_compound_token_env_var(self) -> None:
        """`AWS_SECRET_ACCESS_KEY` のようにトリガー語が識別子に埋め込まれた
        複合トークンの env var もマスクされることを確認する。"""
        text = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        result = secret_masking.mask_secrets(text)
        assert "wJalrXUtnFEMI" not in result
        assert "[REDACTED]" in result

    def test_does_not_mask_max_tokens_numeric_config(self) -> None:
        """`max_tokens=4096` のような非機密の数値設定はマスクしないことを確認する
        （Issue #134 レビュー指摘: token を含む複合キー名の誤検知）。"""
        text = "max_tokens=4096"
        assert secret_masking.mask_secrets(text) == text

    def test_does_not_mask_passwordless_boolean_config(self) -> None:
        """`passwordless=true` のような非機密の真偽値設定はマスクしないことを
        確認する（`password` はトリガー語だが `passwordless` は複合キー）。"""
        text = "passwordless=true"
        assert secret_masking.mask_secrets(text) == text

    def test_masks_bare_password_key_even_if_numeric_value(self) -> None:
        """トリガー語単体のキー（`password` 自体）は、値が数値であっても
        常にマスクすることを確認する（fail-safe を崩さない）。"""
        text = "password=123456"
        result = secret_masking.mask_secrets(text)
        assert "123456" not in result
        assert "[REDACTED]" in result

    def test_masks_compound_key_with_non_numeric_value(self) -> None:
        """複合キーでも値が数値/真偽値でなければ引き続きマスクすることを確認する
        （数値/真偽値スキップの適用範囲が過度に広がらないこと）。"""
        text = "api_secret_token=sk-abcdefghijklmnopqrstuvwxyz"
        result = secret_masking.mask_secrets(text)
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in result
        assert "[REDACTED]" in result

    def test_masks_json_quoted_key(self) -> None:
        """JSON 形式のクォート付きキー（`"password": "..."`）もマスクされることを
        確認する（Issue #134 レビュー指摘: 従来はクォートキー未対応だった）。
        `sk-` 等の他パターンに依存しない値を使い、この KV パターン単体の
        修正を検証する。"""
        text = '{"password": "hunter2plus"}'
        result = secret_masking.mask_secrets(text)
        assert "hunter2plus" not in result
        assert "[REDACTED]" in result

    def test_masks_standalone_jwt(self) -> None:
        header = "eyJ" + "hbGciOiJIUzI1NiJ9"
        payload = "eyJ" + "zdWIiOiIxMjM0In0"
        signature = "c2ln" + "bmF0dXJlLXZhbHVl"
        masked = secret_masking.mask_secrets(f"id: {header}.{payload}.{signature} end")
        for part in (header, payload, signature):
            assert part not in masked
        assert "[REDACTED]" in masked
        assert "end" in masked

    def test_masks_unsigned_jwt(self) -> None:
        header = "eyJ" + "hbGciOiJIUzI1NiJ9"
        payload = "eyJ" + "zdWIiOiIxMjM0In0"
        masked = secret_masking.mask_secrets(f"id: {header}.{payload}. end")
        assert header not in masked
        assert payload not in masked
        assert "[REDACTED]" in masked
        assert "end" in masked

    @pytest.mark.parametrize(
        "text",
        [
            ("eyJ" + "_-") * 16_000,
            "eyJ" + "a" * 80_000,
            "eyJ" + "a." + ("eyJ" + "_-") * 16_000,
        ],
        ids=["repeated-prefix", "long-header", "repeated-payload"],
    )
    def test_masks_incomplete_jwt_like_run_promptly(self, text: str) -> None:
        """JWT が成立しない長い base64url 文字列でも処理が長引かず、元の文字列を保つ。"""
        started = time.monotonic()
        masked = secret_masking.mask_secrets(text)
        assert time.monotonic() - started < 2.0
        assert masked == text

    @pytest.mark.parametrize("prefix", ["gh" + suffix + "_" for suffix in "pousr"])
    @pytest.mark.parametrize("length", [36, 48])
    def test_masks_github_classic_tokens(self, prefix: str, length: int) -> None:
        token = prefix + "A" * length
        masked = secret_masking.mask_secrets(f"id: {token} end")
        assert token not in masked
        assert "A" * length not in masked
        assert "[REDACTED]" in masked

    def test_masks_github_fine_grained_token(self) -> None:
        body = "A" * 22 + "_" + "B" * 59
        token = "github" + "_pat_" + body
        masked = secret_masking.mask_secrets(f"id: {token} end")
        assert token not in masked
        assert body not in masked
        assert "[REDACTED]" in masked

    def test_masks_gitlab_pat(self) -> None:
        body = "A" * 20
        token = "gl" + "pat-" + body
        masked = secret_masking.mask_secrets(f"id: {token} end")
        assert token not in masked
        assert body not in masked
        assert "[REDACTED]" in masked

    @pytest.mark.parametrize("prefix", ["xox" + "b-", "xox" + "p-"])
    def test_masks_slack_tokens(self, prefix: str) -> None:
        body = "1234567890-" * 2 + "AbCdEfGhIjKlMnOpQrStUvWx"
        token = prefix + body
        masked = secret_masking.mask_secrets(f"id: {token} end")
        assert token not in masked
        assert body not in masked
        assert "[REDACTED]" in masked

    @pytest.mark.parametrize(
        ("prefix", "suffix"),
        [
            ("Authorization: ", ""),
            ('{"Authorization": "', '"}'),
            ("Proxy-Authorization: ", ""),
            ("authorization='", "'"),
        ],
    )
    def test_masks_basic_auth_headers(self, prefix: str, suffix: str) -> None:
        credentials = "dXNl" + "cjpwYXNzd29yZA=="
        scheme = "Ba" + "sic"
        masked = secret_masking.mask_secrets(f"{prefix}{scheme} {credentials}{suffix}")
        assert credentials.rstrip("=") not in masked
        assert "[REDACTED]" in masked
        assert masked == f"{prefix}[REDACTED]{suffix}"

    def test_masks_basic_auth_raw_credentials(self) -> None:
        password = "hunter2" + "plus"
        scheme = "Ba" + "sic"
        masked = secret_masking.mask_secrets(f"Authorization: {scheme} user:{password}")
        assert password not in masked
        assert "user:" not in masked
        assert "[REDACTED]" in masked
        assert "Authorization:" in masked

    @pytest.mark.parametrize(
        ("prefix", "value"),
        [("passwd=", "hunter2" + "plus"), ("pwd: ", "hunter2" + "plus"), ("pwd=", "123" + "456")],
    )
    def test_masks_passwd_and_pwd_keys(self, prefix: str, value: str) -> None:
        masked = secret_masking.mask_secrets(prefix + value)
        assert value not in masked
        assert "[REDACTED]" in masked

    @pytest.mark.parametrize("line_break", ["\n", "\r\n", "\r", "\\n", "\\r\\n", "\\r"])
    def test_masks_unterminated_pem_without_consuming_prose(self, line_break: str) -> None:
        begin = "-----BEGIN " + "PRIVATE " + "KEY-----"
        body_lines = ["MIIBogIBAAJ", "c29tZS1mYWtl"]
        text = line_break.join([begin, *body_lines, "next line text"])
        masked = secret_masking.mask_secrets(text)
        assert begin not in masked
        for line in body_lines:
            assert line not in masked
        assert "[REDACTED]" in masked
        assert "next line text" in masked

    def test_masks_unterminated_json_escaped_pem(self) -> None:
        begin = "-----BEGIN " + "PRIVATE " + "KEY-----"
        text = '{"key": "' + begin + "\\nMIIBogIBAAJ\\nc29tZS1mYWtl"
        masked = secret_masking.mask_secrets(text)
        assert begin not in masked
        assert "MIIBogIBAAJ" not in masked
        assert "c29tZS1mYWtl" not in masked
        assert "[REDACTED]" in masked

    @pytest.mark.parametrize("line_break", ["\n", "\r\n", "\r", "\\n", "\\r\\n", "\\r"])
    def test_masks_unterminated_pem_with_legacy_headers(self, line_break: str) -> None:
        begin = "-----BEGIN RSA " + "PRIVATE " + "KEY-----"
        secret_lines = [
            begin,
            "Proc-Type: 4,ENCRYPTED",
            "DEK-Info: DES-EDE3-CBC,ABCDEF0123456789",
            "MIIBogIBAAJ",
            "c29tZS1mYWtl",
        ]
        text = line_break.join([*secret_lines[:3], "", *secret_lines[3:], "next line text"])
        masked = secret_masking.mask_secrets(text)
        for line in secret_lines:
            assert line not in masked
        assert "[REDACTED]" in masked
        assert "next line text" in masked

    def test_masks_encrypted_pem_block(self) -> None:
        begin = "-----BEGIN ENCRYPTED " + "PRIVATE " + "KEY-----"
        end = "-----END ENCRYPTED " + "PRIVATE " + "KEY-----"
        body = "MIIBogIBAAJ"
        masked = secret_masking.mask_secrets(f"{begin}\n{body}\n{end}")
        for part in (begin, body, end):
            assert part not in masked
        assert "[REDACTED]" in masked

    @pytest.mark.parametrize("key", ["secret: ", "password: ", "token=", "credential = "])
    @pytest.mark.parametrize("with_end", [False, True])
    @pytest.mark.parametrize("header_trailing", ["", " ", "\t"])
    def test_masks_unquoted_pem_value_of_trigger_key(
        self, key: str, with_end: bool, header_trailing: str
    ) -> None:
        """トリガー語のキーに引用符なしで PEM が続いても本文が残らない
        （key=value がヘッダー行だけを先に伏せると、PEM として一致しなくなる）。
        BEGIN 行の末尾に空白があっても同じ。"""
        begin = "-----BEGIN " + "PRIVATE " + "KEY-----"
        end = "-----END " + "PRIVATE " + "KEY-----"
        body_lines = ["MIIBogIBAAJ", "c29tZS1mYWtl"]
        lines = [key + begin + header_trailing, *body_lines]
        if with_end:
            lines.append(end)
        masked = secret_masking.mask_secrets("\n".join([*lines, "next line text"]))
        for part in (begin, end, *body_lines):
            assert part not in masked
        assert "[REDACTED]" in masked
        assert "next line text" in masked

    def test_masks_json_escaped_full_pem_block(self) -> None:
        begin = "-----BEGIN " + "PRIVATE " + "KEY-----"
        end = "-----END " + "PRIVATE " + "KEY-----"
        body = "MIIE" + "..."
        text = '{"private_key": "' + begin + "\\n" + body + "\\n" + end + '\\n"}'
        masked = secret_masking.mask_secrets(text)
        for part in (begin, body, end):
            assert part not in masked
        assert "[REDACTED]" in masked

    def test_masks_bearer_token_with_base64_symbols(self) -> None:
        scheme = "Bear" + "er"
        token = "abc" + "+def/ghi=="
        masked = secret_masking.mask_secrets(f"Authorization: {scheme} {token}")
        for part in ("abc", "+def/ghi==", "def/ghi"):
            assert part not in masked
        assert "[REDACTED]" in masked

    @pytest.mark.parametrize("with_payload", [False, True])
    def test_does_not_mask_incomplete_jwt(self, with_payload: bool) -> None:
        text = "eyJ" + "hbGciOiJIUzI1NiJ9"
        if with_payload:
            text += ".eyJ" + "zdWIiOiIxMjM0In0"
        assert secret_masking.mask_secrets(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            "Basic setup is described below",
            "## Basic Usage",
            "xoxo-gossip-girl-forever",
            "run pwd to print the directory",
            "min_pwd_length=12",
            "token_count=123",
        ],
    )
    def test_does_not_mask_non_secret_prose_or_numeric_settings(self, text: str) -> None:
        assert secret_masking.mask_secrets(text) == text

    @pytest.mark.parametrize("prose_prefix", ["", "A" * 50_000])
    def test_masks_long_unterminated_pem_promptly(self, prose_prefix: str) -> None:
        """長い本文や長い英数字から始まる文章でも処理が長引かない。"""
        begin = "-----BEGIN " + "PRIVATE " + "KEY-----"
        body = "A" * 50_000
        prose = prose_prefix + " next line text " * 5_000
        started = time.monotonic()
        masked = secret_masking.mask_secrets(f"{begin}\n{body}\n{prose}")
        assert time.monotonic() - started < 2.0
        assert begin not in masked
        assert masked == "[REDACTED]\n" + prose

    @pytest.mark.parametrize("blank", [" " * 20_000, " \t" * 10_000])
    @pytest.mark.parametrize("tail", ["", ".", "MIIB."])
    def test_masks_unterminated_pem_with_long_blank_line_promptly(
        self, blank: str, tail: str
    ) -> None:
        """END のない PEM の後に長い空白の行が続いても処理が長引かない
        （行内の空白の分け方を全通り試すと、処理時間が空白の長さの 2 乗に伸びる）。"""
        begin = "-----BEGIN " + "PRIVATE " + "KEY-----"
        started = time.monotonic()
        masked = secret_masking.mask_secrets(f"{begin}\n{blank}{tail}")
        assert time.monotonic() - started < 2.0
        assert begin not in masked
        assert "[REDACTED]" in masked


class TestHooksUseSharedModule:
    """各 hook が共通モジュールの `mask_secrets` を利用していることを確認する。"""

    def test_audit_cli_uses_shared_mask_secrets(self) -> None:
        assert audit_cli._mask_secrets is secret_masking.mask_secrets

    def test_audit_prompt_uses_shared_mask_secrets(self) -> None:
        assert audit_prompt._mask_secrets is secret_masking.mask_secrets
