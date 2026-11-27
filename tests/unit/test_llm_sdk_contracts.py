"""llm extra 導入時に実 SDK の HTTP 直列化と例外変換をオフラインで確かめる。"""

from __future__ import annotations

import json
import logging

import pytest
from lib.llm import anthropic_provider, gemini_provider, openai_provider
from lib.llm.types import Attachment, LLMError, LLMRequest


@pytest.mark.parametrize(
    ("provider", "status", "error", "category"),
    [
        *[
            (
                "anthropic",
                429,
                {"type": "rate_limit_error", "details": {"error_code": code}},
                "quota_exhausted",
            )
            for code in (
                "enforced_spend_limit_reached",
                "contract_spend_limit_reached",
                "linked_organization_spend_limit_reached",
            )
        ],
        ("anthropic", 400, {"type": "billing_error"}, "quota_exhausted"),
        ("gemini", 401, {"code": 401, "status": "UNAUTHENTICATED"}, "auth_error"),
        ("gemini", 403, {"code": 403, "status": "PERMISSION_DENIED"}, "auth_error"),
        ("gemini", 429, {"code": 429, "status": "RESOURCE_EXHAUSTED"}, "rate_limited"),
        ("openai", 403, {"code": "permission_denied"}, "auth_error"),
    ],
)
def test_sdk_structured_error_codes_reach_shared_classifiers(
    sdk_environment, sdk_client_factory, provider, status, error, category, caplog
):
    from tests.unit.llm_sdk_support import assert_no_error_details

    caplog.set_level(logging.DEBUG)
    requests = []

    def handle(request):
        requests.append(request)
        return sdk_environment.http.Response(
            status,
            json={"error": {**error, "message": "body-private-responsemarker"}},
            headers={"x-private": "header-private-headermarker"},
        )

    adapter = {
        "anthropic": anthropic_provider,
        "openai": openai_provider,
        "gemini": gemini_provider,
    }[provider]
    request = LLMRequest("test-model", "質問", (), False, 3, 123)
    with pytest.raises(LLMError) as caught:
        adapter.call(
            request,
            "dummy-private-credentialmarker",
            client_factory=sdk_client_factory(provider, handle),
        )

    assert caught.value.category == category
    assert len(requests) == 1
    assert_no_error_details(str(caught.value), caplog.text)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("mode", ["answer", "timeout", "api_error"])
def test_real_sdk_request_and_error_contracts_without_network(
    sdk_environment, provider, mode, caplog
):
    sdk = sdk_environment
    caplog.set_level(logging.DEBUG)
    requests = []
    responses = {
        "anthropic": {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "test-model",
            "content": [{"type": "text", "text": "offline answer"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 2},
        },
        "openai": {
            "id": "resp_test",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": "test-model",
            "output": [
                {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "offline answer", "annotations": []}
                    ],
                }
            ],
        },
        "gemini": {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "offline answer"}]},
                    "finishReason": "STOP",
                }
            ],
        },
    }

    def handle(http_request):
        requests.append(http_request)
        if mode == "timeout":
            raise sdk.http.ReadTimeout("dummy-private-credentialmarker", request=http_request)
        if mode == "api_error":
            return sdk.http.Response(
                500,
                json={
                    "error": {
                        "code": 500,
                        "type": "api_error",
                        "message": "dummy-private-credentialmarker",
                    }
                },
            )
        return sdk.http.Response(200, json=responses[provider])

    transport = sdk.http.MockTransport(handle)

    def factory(**kwargs):
        if provider == "gemini":
            kwargs["http_options"]["client_args"] = {"transport": transport, "trust_env": False}
            return sdk.genai.Client(**kwargs)
        kwargs["http_client"] = sdk.http.Client(transport=transport, trust_env=False)
        return (sdk.anthropic.Anthropic if provider == "anthropic" else sdk.openai.OpenAI)(**kwargs)

    request = LLMRequest(
        "test-model",
        "質問",
        (
            Attachment("image.png", "image/png", b"dummy-image"),
            Attachment("document.pdf", "application/pdf", b"dummy-pdf"),
        ),
        True,
        3,
        123,
    )
    adapter = {
        "anthropic": anthropic_provider,
        "openai": openai_provider,
        "gemini": gemini_provider,
    }[provider]
    if mode == "answer":
        assert (
            adapter.call(request, "dummy-private-credentialmarker", client_factory=factory).text
            == "offline answer"
        )
    else:
        with pytest.raises(LLMError) as caught:
            adapter.call(request, "dummy-private-credentialmarker", client_factory=factory)
        assert caught.value.category == ("timeout" if mode == "timeout" else "api_error")
        assert "dummy-private" not in str(caught.value)
        assert "credentialmarker" not in str(caught.value)

    assert len(requests) == 1
    assert "dummy-private" not in caplog.text
    assert "credentialmarker" not in caplog.text
    body = json.loads(requests[0].content)
    assert "dummy-private" not in requests[0].content.decode()
    if provider == "gemini":
        assert body["tools"] == [{"googleSearch": {}}]
        assert body["generationConfig"]["maxOutputTokens"] == 123
    else:
        assert body["model"] == "test-model"
        assert body["tools"][0]["type"].startswith("web_search")
