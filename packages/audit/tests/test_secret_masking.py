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
