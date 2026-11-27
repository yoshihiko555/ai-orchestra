"""実SDKのオフライン検証で共有するfixtureと応答データ。"""

from __future__ import annotations

import base64
import json
import socket
from types import SimpleNamespace

import pytest


@pytest.fixture
def sdk_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_GENAI_USE_ENTERPRISE",
    ):
        monkeypatch.delenv(name, raising=False)

    def reject_network(*args, **kwargs):
        raise AssertionError("SDK テストで実ネットワークに接続してはいけません。")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    return SimpleNamespace(
        http=pytest.importorskip("httpx2", reason="実 SDK の確認には llm extra が必要"),
        anthropic=pytest.importorskip("anthropic", reason="実 SDK の確認には llm extra が必要"),
        openai=pytest.importorskip("openai", reason="実 SDK の確認には llm extra が必要"),
        genai=pytest.importorskip("google.genai", reason="実 SDK の確認には llm extra が必要"),
    )


@pytest.fixture
def sdk_client_factory(sdk_environment):
    def build(provider, handler):
        sdk = sdk_environment
        transport = sdk.http.MockTransport(handler)

        def factory(**kwargs):
            if provider == "gemini":
                kwargs["http_options"]["client_args"] = {
                    "transport": transport,
                    "trust_env": False,
                }
                return sdk.genai.Client(**kwargs)
            kwargs["http_client"] = sdk.http.Client(transport=transport, trust_env=False)
            constructor = sdk.anthropic.Anthropic if provider == "anthropic" else sdk.openai.OpenAI
            return constructor(**kwargs)

        return factory

    return build


@pytest.fixture
def sdk_tool_client_factory(sdk_environment):
    from any_llm import AnyLLM

    def build(provider, handler):
        sdk = sdk_environment
        transport = sdk.http.MockTransport(handler)

        def factory(provider_name, **kwargs):
            assert provider_name == provider
            if provider == "gemini":
                kwargs["http_options"] = {
                    "client_args": {"transport": transport, "trust_env": False},
                    "async_client_args": {"transport": transport, "trust_env": False},
                }
            else:
                kwargs["http_client"] = sdk.http.AsyncClient(transport=transport, trust_env=False)
            return AnyLLM.create(provider_name, **kwargs)

        return factory

    return build


def quota_details(quota_id):
    return {
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{"quotaId": quota_id}],
    }


def error_response(provider, category):
    message = "body-private-responsemarker dummy-private-credentialmarker"
    if provider == "anthropic":
        status, kind = {
            "quota_exhausted": (402, "billing_error"),
            "rate_limited": (429, "rate_limit_error"),
            "auth_error": (401, "authentication_error"),
        }[category]
        return status, {"type": "error", "error": {"type": kind, "message": message}}
    if provider == "openai":
        status, code = {
            "quota_exhausted": (429, "insufficient_quota"),
            "rate_limited": (429, "rate_limit_exceeded"),
            "auth_error": (401, "invalid_api_key"),
        }[category]
        return status, {"error": {"code": code, "type": code, "message": message}}
    if category == "auth_error":
        status, kind = 400, "INVALID_ARGUMENT"
        detail = {
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": "API_KEY_INVALID",
        }
    else:
        status, kind = 429, "RESOURCE_EXHAUSTED"
        detail = quota_details(
            "GenerateRequestsPerDayPerProject"
            if category == "quota_exhausted"
            else "GenerateRequestsPerMinutePerProject"
        )
    return status, {
        "error": {"code": status, "status": kind, "message": message, "details": [detail]}
    }


def assert_no_error_details(*outputs):
    for output in outputs:
        for line in output.splitlines():
            for marker in (
                "dummy-private",
                "credentialmarker",
                "body-private",
                "responsemarker",
                "header-private",
                "headermarker",
            ):
                assert marker not in line.casefold()


def tool_response(provider, path, *, final):
    calls = [
        {"id": "call_first", "name": "lookup_value", "arguments": {"value": 1}},
        {"id": "call_second", "name": "lookup_value", "arguments": {"value": 2}},
    ]
    if provider == "anthropic":
        return {
            "id": "msg_offline",
            "type": "message",
            "role": "assistant",
            "model": "test-model",
            "content": [{"type": "text", "text": "確認完了"}]
            if final
            else [
                {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                for c in calls
            ],
            "stop_reason": "end_turn" if final else "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
    if provider == "gemini":
        return {
            "usageMetadata": {
                "promptTokenCount": 1,
                "candidatesTokenCount": 2,
                "totalTokenCount": 3,
            },
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "確認完了"}]
                        if final
                        else [
                            {
                                "functionCall": {
                                    "id": c["id"],
                                    "name": c["name"],
                                    "args": c["arguments"],
                                },
                                "thoughtSignature": base64.b64encode(c["id"].encode()).decode(),
                            }
                            for c in calls
                        ],
                    },
                    "finishReason": "STOP",
                }
            ],
        }
    if path.endswith("/chat/completions"):
        return {
            "id": "chatcmpl_offline",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop" if final else "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "確認完了" if final else None,
                        "tool_calls": []
                        if final
                        else [
                            {
                                "id": c["id"],
                                "type": "function",
                                "function": {
                                    "name": c["name"],
                                    "arguments": json.dumps(c["arguments"]),
                                },
                            }
                            for c in calls
                        ],
                    },
                }
            ],
        }
    assert path.endswith("/responses")
    return {
        "id": "resp_offline",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": "test-model",
        "output": [
            {
                "id": "msg_offline",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "確認完了", "annotations": []}],
            }
        ]
        if final
        else [
            {
                "type": "function_call",
                "id": "fc_" + c["id"],
                "call_id": c["id"],
                "name": c["name"],
                "arguments": json.dumps(c["arguments"]),
                "status": "completed",
            }
            for c in calls
        ],
    }
