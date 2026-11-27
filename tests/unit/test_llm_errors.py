"""SDK の機械向けコードから分類し、CLI の終了コードまで伝える。"""

from __future__ import annotations

import importlib

import pytest
from lib.llm.types import LLMError

from tests.unit.llm_sdk_support import quota_details


class UnprintableSDKError(Exception):
    """SDK 例外を文字列にする実装を検出する。"""

    def __str__(self):
        raise AssertionError("SDK 例外を文字列にしてはいけません。")

    def __repr__(self):
        raise AssertionError("SDK 例外を repr にしてはいけません。")


class UnprintableDetails(dict):
    """構造化コードの参照は許可し、本文全体の文字列化は拒否する。"""

    def __str__(self):
        raise AssertionError("応答本文を文字列にしてはいけません。")

    def __repr__(self):
        raise AssertionError("応答本文を repr にしてはいけません。")


@pytest.mark.parametrize(
    ("provider", "attributes", "category"),
    [
        ("openai", {"status_code": 429, "code": "insufficient_quota"}, "quota_exhausted"),
        ("openai", {"status_code": 429, "code": "rate_limit_exceeded"}, "rate_limited"),
        ("openai", {"status_code": 429}, "rate_limited"),
        ("openai", {"status_code": 401}, "auth_error"),
        ("openai", {"status_code": 403}, "auth_error"),
        ("anthropic", {"status_code": 402}, "quota_exhausted"),
        ("anthropic", {"status_code": 400, "type": "billing_error"}, "quota_exhausted"),
        *[
            ("anthropic", {"status_code": 429, "error_code": code}, "quota_exhausted")
            for code in (
                "enforced_spend_limit_reached",
                "contract_spend_limit_reached",
                "linked_organization_spend_limit_reached",
            )
        ],
        ("anthropic", {"status_code": 429, "type": "rate_limit_error"}, "rate_limited"),
        ("anthropic", {"status_code": 401}, "auth_error"),
        ("anthropic", {"status_code": 403}, "auth_error"),
        *[
            (
                "gemini",
                {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "details": {"error": {"details": [quota_details(quota_id)]}},
                },
                category,
            )
            for quota_id, category in (
                ("GenerateRequestsPerDayPerProject", "quota_exhausted"),
                ("GenerateRequestsPerMinutePerProject", "rate_limited"),
            )
        ],
        ("gemini", {"code": 429, "status": "RESOURCE_EXHAUSTED"}, "rate_limited"),
        ("gemini", {"code": 401}, "auth_error"),
        ("gemini", {"code": 403}, "auth_error"),
        (
            "gemini",
            {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "details": {
                    "error": {
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                "reason": "API_KEY_INVALID",
                            }
                        ]
                    }
                },
            },
            "auth_error",
        ),
        *[(provider, {}, "api_error") for provider in ("anthropic", "openai", "gemini")],
    ],
)
def test_error_classification_uses_codes_without_stringifying_exception_or_details(
    provider, attributes, category
):
    errors = importlib.import_module("lib.llm.errors")
    classify = getattr(errors, f"classify_{provider}")
    messages = []
    for message in ("credit balance insufficient_quota API_KEY_INVALID", "unrelated failure"):
        exc = UnprintableSDKError(message)
        exc.__dict__.update(attributes)
        exc.details = UnprintableDetails(attributes.get("details", {}))
        exc.body = UnprintableDetails({"message": message})

        result = classify(exc)

        assert isinstance(result, LLMError)
        assert result.category == category
        assert str(result)
        assert message not in str(result)
        messages.append(str(result))
    assert messages[0] == messages[1]


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_error_classifier_preserves_timeout_and_generic_api_error(provider):
    errors = importlib.import_module("lib.llm.errors")
    classify = getattr(errors, f"classify_{provider}")

    assert classify(TimeoutError("非公開の詳細")).category == "timeout"
    exc = UnprintableSDKError("insufficient_quota credit balance API_KEY_INVALID")
    exc.status_code = 500
    exc.code = 500 if provider == "gemini" else "server_error"
    assert classify(exc).category == "api_error"
