"""Sec11-3-6 L2/L3: proposer 出力経路の secret 検知 (proposer_security) 単体テスト。"""

from __future__ import annotations

import base64
import urllib.parse

from tests.module_loader import load_module

psec = load_module(
    "meta_harness_proposer_security_test",
    "packages/meta-harness/lib/proposer_security.py",
)

_CANARY = "meta-harness-canary-refresh-deadbeefdeadbeefdeadbeefdeadbeef"


def _sample_jwt() -> str:
    """secret scanner に触れる JWT リテラルを置かず、検査用の 3 セグメント値を返す。"""
    segments = [
        base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("="),
        base64.urlsafe_b64encode(b'{"sub":"1234567890"}').decode().rstrip("="),
        base64.urlsafe_b64encode(b"signature-placeholder-bytes").decode().rstrip("="),
    ]
    return ".".join(segments)


def _sample_sk_key(key_kind: str | None = None) -> str:
    """secret scanner に触れるキーリテラルを置かず、検査用 sk- key を返す。"""
    parts = ["sk"]
    if key_kind:
        parts.append(key_kind)
    parts.append("abcdef0123456789ABCDEF")
    return "-".join(parts)


class TestCanaryDetection:
    def test_plaintext_variant_detected(self) -> None:
        text = f"stolen credential: {_CANARY} end"
        # この canary は URL-safe 文字のみのため url 変形は平文と一致する。
        assert "plaintext" in psec.scan_text_for_canary(text, _CANARY)

    def test_base64_variant_detected(self) -> None:
        encoded = base64.b64encode(_CANARY.encode()).decode()
        hits = psec.scan_text_for_canary(f"blob={encoded}", _CANARY)
        assert "base64" in hits

    def test_hex_variant_detected(self) -> None:
        encoded = _CANARY.encode().hex()
        hits = psec.scan_text_for_canary(f"data:{encoded}", _CANARY)
        assert "hex" in hits

    def test_url_variant_detected(self) -> None:
        # URL エンコードで必ず変化する値を持つ canary を使う（空白入りは quote で %20 化）。
        canary = "canary token/with+special"
        encoded = urllib.parse.quote(canary, safe="")
        hits = psec.scan_text_for_canary(f"q={encoded}", canary)
        assert "url" in hits

    def test_no_canary_when_absent(self) -> None:
        assert psec.scan_text_for_canary("clean overlay content", _CANARY) == []

    def test_empty_canary_is_ignored(self) -> None:
        assert psec.scan_text_for_canary("anything", "") == []
        assert psec.scan_text_for_canary("anything", None) == []


class TestSecretScan:
    def test_sk_api_key_detected(self) -> None:
        hits = psec.scan_text_for_secrets(f"token = {_sample_sk_key()}")
        assert "API key (sk- prefix)" in hits

    def test_hyphenated_sk_api_keys_detected(self) -> None:
        for key_kind in ("proj", "svcacct"):
            hits = psec.scan_text_for_secrets(f"token = {_sample_sk_key(key_kind)}")
            assert "OpenAI API key (hyphenated sk- prefix)" in hits

    def test_jwt_three_segment_detected(self) -> None:
        jwt = _sample_jwt()
        hits = psec.scan_text_for_secrets(f"access_token={jwt}")
        assert "JWT (3-segment)" in hits

    def test_clean_text_has_no_secrets(self) -> None:
        assert psec.scan_text_for_secrets("# Example\n\nImproved facet.\n") == []

    def test_canary_value_is_not_a_generic_secret(self) -> None:
        # canary 自体は L3 の汎用パターンに一致してはならない（誤検知防止）。
        assert psec.scan_text_for_secrets(_CANARY) == []


class TestRedactForStorage:
    def test_jwt_is_masked(self) -> None:
        jwt = _sample_jwt()
        redacted = psec.redact_for_storage(f"access_token={jwt}")
        assert jwt not in redacted
        assert "[REDACTED:JWT (3-segment)]" in redacted

    def test_hyphenated_sk_api_key_is_masked(self) -> None:
        key = _sample_sk_key("proj")
        redacted = psec.redact_for_storage(f"api_key={key}")
        assert key not in redacted
        assert "[REDACTED:OpenAI API key (hyphenated sk- prefix)]" in redacted

    def test_canary_variants_are_masked(self) -> None:
        encoded = base64.b64encode(_CANARY.encode()).decode()
        text = f"plain={_CANARY} b64={encoded}"
        redacted = psec.redact_for_storage(text, auth_canary=_CANARY)
        assert _CANARY not in redacted
        assert encoded not in redacted
        assert "[REDACTED:auth canary" in redacted

    def test_generic_secret_still_masked_without_canary(self) -> None:
        key = _sample_sk_key()
        redacted = psec.redact_for_storage(f"key {key}")
        assert key not in redacted


class TestScanNamedTexts:
    def test_canary_hit_reports_l2_detector(self) -> None:
        named = {"overlay:facets/a/SKILL.md": f"leak {_CANARY}"}
        violations = psec.scan_named_texts(named, auth_canary=_CANARY)
        assert len(violations) == 1
        assert violations[0].detector == psec.DETECTOR_CANARY
        assert "facets/a/SKILL.md" in violations[0].reason

    def test_secret_hit_reports_l3_detector(self) -> None:
        named = {"proposal": _sample_sk_key()}
        violations = psec.scan_named_texts(named, auth_canary=_CANARY)
        assert len(violations) == 1
        assert violations[0].detector == psec.DETECTOR_SECRET_SCAN

    def test_canary_takes_precedence_over_secret_scan_in_same_text(self) -> None:
        named = {"proposal": f"{_CANARY} {_sample_sk_key()}"}
        violations = psec.scan_named_texts(named, auth_canary=_CANARY)
        assert [v.detector for v in violations] == [psec.DETECTOR_CANARY]

    def test_clean_texts_produce_no_violations(self) -> None:
        named = {"proposal": "{}", "overlay:facets/a/SKILL.md": "clean"}
        assert psec.scan_named_texts(named, auth_canary=_CANARY) == []
