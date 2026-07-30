"""secret_masking.py の共通マスキングロジックのテスト。"""

from __future__ import annotations

import sys

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

    def test_does_not_mask_token_count_numeric_config(self) -> None:
        """`token_count=123` のような非機密の数値設定はマスクしないことを確認する。"""
        text = "token_count=123"
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

    def test_masks_bare_secret_key_even_if_boolean_value(self) -> None:
        text = "secret=true"
        result = secret_masking.mask_secrets(text)
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


class TestHooksUseSharedModule:
    """各 hook が共通モジュールの `mask_secrets` を利用していることを確認する。"""

    def test_audit_cli_uses_shared_mask_secrets(self) -> None:
        assert audit_cli._mask_secrets is secret_masking.mask_secrets

    def test_audit_prompt_uses_shared_mask_secrets(self) -> None:
        assert audit_prompt._mask_secrets is secret_masking.mask_secrets

    def test_audit_cli_masks_sas_token(self) -> None:
        """audit-cli.py 経由でも SAS トークンがマスクされることを確認する（旧仕様の維持）。"""
        text = "SharedAccessSignature=sv=2020&sig=abc123"
        assert "[REDACTED]" in audit_cli._mask_secrets(text)

    def test_audit_prompt_masks_pem_block(self) -> None:
        """audit-prompt.py 経由でも PEM 秘密鍵ブロック全体がマスクされることを確認する（従来欠落していた挙動）。"""
        rsa_begin = "-----BEGIN RSA " + "PRIVATE " + "KEY-----"
        rsa_end = "-----END RSA " + "PRIVATE " + "KEY-----"
        body_line = "MIIBogIBAAJ..."
        text = f"{rsa_begin}\n{body_line}\n{rsa_end}"
        result = audit_prompt._mask_secrets(text)
        assert rsa_begin not in result
        assert body_line not in result
        assert rsa_end not in result
