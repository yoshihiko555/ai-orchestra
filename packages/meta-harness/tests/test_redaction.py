"""redaction のテスト（EV-10, Sec2-6, Sec7「redaction」）。

`packages/meta-harness/lib/redaction.py` は `packages/codex-harness/scripts/harness_common.py`
の `REDACTION_PATTERNS` を意図的に verbatim 複製したもの（両モジュールの docstring 参照）。
同値性が崩れていないかを直接比較で検証する。
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.module_loader import load_module

redaction = load_module(
    "meta_harness_redaction",
    "packages/meta-harness/lib/redaction.py",
)
harness_common = load_module(
    "codex_harness_common_for_redaction_test",
    "packages/codex-harness/scripts/harness_common.py",
)


def _as_comparable(patterns: list[tuple[str, re.Pattern]]) -> list[tuple[str, str, int]]:
    return [(name, pattern.pattern, pattern.flags) for name, pattern in patterns]


class TestRedactionPatternsEquivalence:
    def test_meta_harness_patterns_match_codex_harness_patterns_exactly(self) -> None:
        meta_patterns = _as_comparable(redaction.REDACTION_PATTERNS)
        codex_patterns = _as_comparable(harness_common.REDACTION_PATTERNS)

        assert meta_patterns == codex_patterns

    def test_equivalence_check_actually_detects_drift(self) -> None:
        """上記の同値性テストが本当にドリフトを検出できることを自己検証する。

        実ファイルには触れず、メモリ上のコピーを改変して比較関数の感度を確認する。
        """
        meta_patterns = _as_comparable(redaction.REDACTION_PATTERNS)
        drifted = list(meta_patterns)
        drifted[0] = (drifted[0][0], drifted[0][1] + "EXTRA", drifted[0][2])

        assert drifted != meta_patterns
        codex_patterns = _as_comparable(harness_common.REDACTION_PATTERNS)
        assert drifted != codex_patterns


class TestRedactSecretsMasking:
    def test_openai_api_key_assignment_is_masked(self) -> None:
        text = "OPENAI_API_KEY=sk-abcdef1234567890abcdef"
        result = redaction.redact_secrets(text)
        assert "sk-abcdef1234567890abcdef" not in result
        assert "[REDACTED:OPENAI_API_KEY assignment]" in result

    def test_broker_run_token_is_masked(self) -> None:
        """Issue #354: broker の per-run トークン（mh- + token_urlsafe(24) = 32 文字）が
        judge 失敗時の stdout 抜粋経由で artifacts へ永続化されうるため、mh- prefix の
        トークン形式をマスクする。"""
        token = "mh-" + "Ab3dEf6hIj9lMn2pQr5tUv8xYz1B4c6d"
        result = redaction.redact_secrets(f"ANTHROPIC_API_KEY={token} rejected")
        assert token not in result
        assert "[REDACTED:meta-harness broker run token (mh-)]" in result

    def test_short_mh_prefixed_names_are_not_masked(self) -> None:
        """mh-run- 等の短いコンテナ名・エイリアスまで塗りつぶさないこと（20 文字未満）。"""
        text = "container mh-run-abc123 via mh-broker alias"
        assert redaction.redact_secrets(text) == text

    def test_mh_token_minimum_length_boundary(self) -> None:
        """PR #355 レビュー指摘: 最小長 `_MIN_TOKEN_LENGTH` の境界で挙動を固定する
        （ちょうど N 文字はマスク、N-1 文字は非マスク）。"""
        n = redaction._MIN_TOKEN_LENGTH
        at_boundary = "mh-" + "a" * n
        below_boundary = "mh-" + "a" * (n - 1)
        assert at_boundary not in redaction.redact_secrets(f"x {at_boundary} y")
        assert redaction.redact_secrets(f"x {below_boundary} y") == f"x {below_boundary} y"

    def test_github_pat_is_masked(self) -> None:
        token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
        result = redaction.redact_secrets(f"token={token}")
        assert token not in result
        assert "[REDACTED:GitHub PAT (ghp_)]" in result

    def test_github_fine_grained_pat_is_masked(self) -> None:
        token = "github_pat_" + "a1B2c3D4e5F6g7H8i9J0k1L2"
        result = redaction.redact_secrets(f"token={token}")
        assert token not in result
        assert "[REDACTED:GitHub fine-grained PAT (github_pat_)]" in result

    def test_sk_prefixed_api_key_is_masked(self) -> None:
        key = "sk-" + "abcdefghij"
        result = redaction.redact_secrets(f"key={key}")
        assert key not in result
        assert "[REDACTED:API key (sk- prefix)]" in result

    def test_aws_access_key_id_is_masked(self) -> None:
        key = "AKIA" + "ABCD1234EFGH5678"
        result = redaction.redact_secrets(f"id={key}")
        assert key not in result
        assert "[REDACTED:AWS_ACCESS_KEY_ID value]" in result

    def test_aws_secret_access_key_assignment_is_masked(self) -> None:
        text = "AWS_SECRET_ACCESS_KEY=abcdEFGH12345/secretvalue"
        result = redaction.redact_secrets(text)
        assert "abcdEFGH12345/secretvalue" not in result
        assert "[REDACTED:AWS_SECRET_ACCESS_KEY assignment]" in result

    def test_github_token_assignment_is_masked(self) -> None:
        text = "GITHUB_TOKEN=some-secret-value-here"
        result = redaction.redact_secrets(text)
        assert "some-secret-value-here" not in result
        assert "[REDACTED:GITHUB_TOKEN assignment]" in result

    def test_pem_private_key_block_is_masked(self) -> None:
        pem = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIBVQIBADANBgkqhkiG9w0BAQEFAASCAT8wggE7AgEAAkEA\n"
            "-----END PRIVATE KEY-----"
        )
        result = redaction.redact_secrets(f"before\n{pem}\nafter")
        assert "MIIBVQIBADANBgkqhkiG9w0BAQEFAASCAT8wggE7AgEAAkEA" not in result
        assert "[REDACTED:PEM private key block]" in result
        assert "before" in result
        assert "after" in result

    def test_clean_text_is_unchanged(self) -> None:
        text = "this is a perfectly normal log line with no secrets"
        assert redaction.redact_secrets(text) == text


class TestRedactFileInPlace:
    def test_file_with_secret_is_rewritten(self, tmp_path: Path) -> None:
        path = tmp_path / "progress.log"
        path.write_text("OPENAI_API_KEY=sk-abcdef1234567890abcdef\n", encoding="utf-8")

        redaction.redact_file_in_place(path)

        content = path.read_text(encoding="utf-8")
        assert "sk-abcdef1234567890abcdef" not in content
        assert "[REDACTED:" in content

    def test_clean_file_is_not_rewritten(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "clean.log"
        path.write_text("nothing secret here\n", encoding="utf-8")

        def _fail_if_called(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("write_atomic should not be called for unchanged content")

        monkeypatch.setattr(redaction, "write_atomic", _fail_if_called)

        redaction.redact_file_in_place(path)  # should not raise

    def test_missing_file_is_a_noop(self, tmp_path: Path) -> None:
        redaction.redact_file_in_place(tmp_path / "does-not-exist.log")  # should not raise
