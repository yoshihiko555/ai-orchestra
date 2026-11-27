"""道具付きの1往復と、呼び出し結果を次の往復へ渡す契約。"""

from __future__ import annotations

import base64
import importlib
import json
import logging
import subprocess
import sys
from dataclasses import replace
from unittest.mock import Mock

import pytest
from lib.llm.types import LLMError

from tests.module_loader import REPO_ROOT
from tests.unit.llm_sdk_support import assert_no_error_details, error_response, tool_response


def test_tool_turn_response_requires_explicit_usage():
    from lib.llm.tool_types import AssistantMessage, TokenUsage, ToolTurnResponse

    message = AssistantMessage("回答")
    with pytest.raises(TypeError, match="usage"):
        ToolTurnResponse(message)

    response = ToolTurnResponse(message, usage=TokenUsage(1, 2))
    assert response.message == message
    assert response.usage == TokenUsage(1, 2)


def tool_request():
    tools = importlib.import_module("lib.llm.tool_types")
    return tools.ToolTurnRequest(
        model="test-model",
        messages=(tools.UserMessage(text="2つの値を調べて"),),
        tools=(
            tools.ToolSpec(
                name="lookup_value",
                description="指定された値を照会する",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                },
            ),
        ),
        timeout_sec=3,
        max_output_tokens=123,
    )


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_tool_turn_preserves_calls_and_transfers_results_to_next_turn(
    sdk_environment, sdk_tool_client_factory, provider
):
    tools = importlib.import_module("lib.llm.tool_types")
    from lib.llm.providers import call_tool_turn

    requests = []

    def handle(request):
        requests.append(request)
        assert len(requests) <= 2, "1回の入口で自動的に道具を実行してはいけません。"
        return sdk_environment.http.Response(
            200, json=tool_response(provider, request.url.path, final=len(requests) == 2)
        )

    factory = sdk_tool_client_factory(provider, handle)
    request = tool_request()
    first = call_tool_turn(provider, request, "dummy-key", client_factory=factory)

    assert len(requests) == 1
    calls = first.message.tool_calls
    assert [(c.call_id, c.name, c.arguments) for c in calls] == [
        ("call_first", "lookup_value", {"value": 1}),
        ("call_second", "lookup_value", {"value": 2}),
    ]
    results = tools.ToolResultsMessage(
        results=(
            tools.ToolResult(call_id="call_second", content="second-result", is_error=False),
            tools.ToolResult(call_id="call_first", content="first-result", is_error=False),
        )
    )
    second = call_tool_turn(
        provider,
        replace(request, messages=(*request.messages, first.message, results)),
        "dummy-key",
        client_factory=factory,
    )

    assert len(requests) == 2
    assert second.message.text == "確認完了"
    assert not second.message.tool_calls
    initial, following = [json.loads(r.content) for r in requests]
    if provider == "anthropic":
        assert initial["tools"][0]["input_schema"] == request.tools[0].parameters
        assert initial["tools"][0]["name"] == "lookup_value"
        outputs = following["messages"][-1]["content"]
        assert {c["tool_use_id"]: c["content"] for c in outputs} == {
            "call_first": "first-result",
            "call_second": "second-result",
        }
        assert (
            following["messages"][-2]["content"]
            == tool_response(provider, requests[0].url.path, final=False)["content"]
        )
    elif provider == "gemini":
        declaration = initial["tools"][0]["functionDeclarations"][0]
        assert declaration["name"] == "lookup_value"
        assert declaration["parameters"]["required"] == ["value"]
        assert declaration["parameters"]["type"].casefold() == "object"
        assert declaration["parameters"]["properties"]["value"]["type"].casefold() == "integer"
        parts = following["contents"][-2]["parts"]
        assert [p["functionCall"] for p in parts] == [
            {"id": c.call_id, "name": c.name, "args": c.arguments} for c in calls
        ]
        assert [p["thoughtSignature"] for p in parts] == [
            base64.b64encode(c.call_id.encode()).decode() for c in calls
        ]
        outputs = [p["functionResponse"] for p in following["contents"][-1]["parts"]]
        by_id = {p["id"]: p["response"] for p in outputs}
        assert "first-result" in by_id["call_first"].values()
        assert "second-result" in by_id["call_second"].values()
        assert all(p["name"] == "lookup_value" for p in outputs)
    elif requests[0].url.path.endswith("/chat/completions"):
        assert initial["store"] is False
        assert following["store"] is False
        assert initial["tools"][0]["function"]["name"] == "lookup_value"
        assert initial["tools"][0]["function"]["parameters"] == request.tools[0].parameters
        outputs = [m for m in following["messages"] if m["role"] == "tool"]
        assert {m["tool_call_id"]: m["content"] for m in outputs} == {
            "call_first": "first-result",
            "call_second": "second-result",
        }
        assistant = next(m for m in following["messages"] if m["role"] == "assistant")
        assert [c["id"] for c in assistant["tool_calls"]] == [c.call_id for c in calls]
        assert [
            (c["function"]["name"], json.loads(c["function"]["arguments"]))
            for c in assistant["tool_calls"]
        ] == [(c.name, c.arguments) for c in calls]
    else:
        assert initial["tools"][0]["name"] == "lookup_value"
        assert initial["tools"][0]["parameters"] == request.tools[0].parameters
        assert initial["store"] is False
        assert following["store"] is False
        outputs = [m for m in following["input"] if m.get("type") == "function_call_output"]
        assert {m["call_id"]: m["output"] for m in outputs} == {
            "call_first": "first-result",
            "call_second": "second-result",
        }
        prior = [m for m in following["input"] if m.get("type") == "function_call"]
        assert [(m["id"], m["call_id"]) for m in prior] == [
            ("fc_call_first", "call_first"),
            ("fc_call_second", "call_second"),
        ]
        assert [(m["name"], json.loads(m["arguments"])) for m in prior] == [
            (c.name, c.arguments) for c in calls
        ]


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("category", ["quota_exhausted", "rate_limited", "auth_error"])
def test_tool_turn_classifies_sdk_error_without_retry_or_private_output(
    sdk_environment, sdk_tool_client_factory, provider, category, capsys, caplog
):
    from lib.llm.providers import call_tool_turn

    caplog.set_level(logging.DEBUG)
    requests = []
    status, payload = error_response(provider, category)

    def handle(request):
        requests.append(request)
        return sdk_environment.http.Response(
            status, json=payload, headers={"x-private": "header-private-headermarker"}
        )

    factory = sdk_tool_client_factory(provider, handle)
    with pytest.raises(LLMError) as caught:
        call_tool_turn(
            provider, tool_request(), "dummy-private-credentialmarker", client_factory=factory
        )

    captured = capsys.readouterr()
    assert_no_error_details(str(caught.value), captured.out, captured.err, caplog.text)
    assert caught.value.category == category
    assert len(requests) == 1


def test_shared_tool_api_and_auth_start_without_sdk_imports(tmp_path):
    script = """
import importlib.abc
import sys
sys.path[:0] = [sys.argv[1] + '/scripts', sys.argv[1] + '/packages/core/hooks']
class RejectSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'anthropic', 'openai', 'google', 'any_llm', 'litellm'}:
            raise AssertionError('SDK の import は呼び出し時まで遅延する必要があります。')
sys.meta_path.insert(0, RejectSDK())
from lib.llm.tool_types import ToolSpec, UserMessage, ToolTurnRequest
from lib.llm import errors, retry, providers
from lib.llm import agent_loop, agent_answer, run_output
from lib.llm_cli import run_auth_status
request = ToolTurnRequest(
    model='test-model', messages=(UserMessage(text='質問'),),
    tools=(ToolSpec(name='custom_tool', description='説明', parameters={'type': 'object'}),),
    timeout_sec=1, max_output_tokens=16,
)
assert request.tools[0].name == 'custom_tool'
assert run_auth_status() == 0
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(REPO_ROOT)],
        env={"HOME": str(tmp_path), "AI_ORCHESTRA_DIR": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert all(provider in result.stdout for provider in ("anthropic", "openai", "gemini"))


@pytest.mark.parametrize("invalid_arguments", ["[]", "{invalid-json"])
def test_tool_turn_rejects_non_object_or_malformed_arguments(
    sdk_environment, sdk_tool_client_factory, invalid_arguments
):
    from lib.llm.providers import call_tool_turn

    def handle(request):
        payload = tool_response("openai", request.url.path, final=False)
        payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
            invalid_arguments
        )
        return sdk_environment.http.Response(200, json=payload)

    with pytest.raises(LLMError) as caught:
        call_tool_turn(
            "openai",
            tool_request(),
            "dummy-key",
            client_factory=sdk_tool_client_factory("openai", handle),
        )

    assert caught.value.category == "api_error"
    assert invalid_arguments not in str(caught.value)


@pytest.mark.parametrize("invalid_state", ["different_provider", "unmatched_result"])
def test_tool_turn_rejects_invalid_history_before_creating_client(invalid_state):
    from lib.llm import tool_types as tools
    from lib.llm.providers import call_tool_turn

    message = (
        tools.AssistantMessage("", provider_state=("gemini", "{}"))
        if invalid_state == "different_provider"
        else tools.ToolResultsMessage((tools.ToolResult("unknown", "結果", False),))
    )
    factory = Mock()
    with pytest.raises(LLMError) as caught:
        call_tool_turn(
            "openai",
            replace(tool_request(), messages=(message,)),
            "dummy-key",
            client_factory=factory,
        )

    assert caught.value.category == "api_error"
    factory.assert_not_called()


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_tool_turn_transfers_error_result_with_its_call_id(
    sdk_environment, sdk_tool_client_factory, provider
):
    from lib.llm import tool_types as tools
    from lib.llm.providers import call_tool_turn

    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return sdk_environment.http.Response(
            200, json=tool_response(provider, request.url.path, final=True)
        )

    request = replace(
        tool_request(),
        messages=(
            tools.UserMessage("調べて"),
            tools.AssistantMessage(
                "", (tools.ToolCall("failed-call", "lookup_value", {"value": 1}),)
            ),
            tools.ToolResultsMessage((tools.ToolResult("failed-call", "照会失敗", True),)),
        ),
    )
    response = call_tool_turn(
        provider, request, "dummy-key", client_factory=sdk_tool_client_factory(provider, handle)
    )

    assert response.message.text == "確認完了"
    assert len(requests) == 1
    body = requests[0]
    if provider == "anthropic":
        result = body["messages"][-1]["content"][0]
        assert result["tool_use_id"] == "failed-call"
        assert json.loads(result["content"]) == {"error": "照会失敗"}
    elif provider == "openai":
        result = body["messages"][-1]
        assert result["tool_call_id"] == "failed-call"
        assert json.loads(result["content"]) == {"error": "照会失敗"}
    else:
        result = body["contents"][-1]["parts"][0]["functionResponse"]
        assert result["id"] == "failed-call"
        assert result["response"] == {"error": "照会失敗"}


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("failed", [False, True])
def test_tool_turn_releases_client_on_success_and_failure(
    sdk_environment, sdk_tool_client_factory, provider, failed
):
    from lib.llm.providers import call_tool_turn

    def handle(request):
        status, payload = (
            error_response(provider, "auth_error")
            if failed
            else (200, tool_response(provider, request.url.path, final=True))
        )
        return sdk_environment.http.Response(status, json=payload)

    constructor = sdk_tool_client_factory(provider, handle)
    closers = []

    def factory(*args, **kwargs):
        assert kwargs["max_retries"] == 0
        assert kwargs["unified_exceptions"] is False
        instance = constructor(*args, **kwargs)
        close = Mock(wraps=instance.client.close)
        instance.client.close = close
        closers.append(close)
        return instance

    if failed:
        with pytest.raises(LLMError) as caught:
            call_tool_turn(provider, tool_request(), "dummy-key", client_factory=factory)
        assert caught.value.category == "auth_error"
    else:
        assert (
            call_tool_turn(
                provider, tool_request(), "dummy-key", client_factory=factory
            ).message.text
            == "確認完了"
        )
    assert len(closers) == 1
    closers[0].assert_called_once_with()


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_tool_turn_returns_input_and_output_usage(
    sdk_environment, sdk_tool_client_factory, provider
):
    from lib.llm.providers import call_tool_turn

    def handle(request):
        payload = tool_response(provider, request.url.path, final=True)
        if provider == "openai":
            payload["usage"] = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
        return sdk_environment.http.Response(200, json=payload)

    response = call_tool_turn(
        provider,
        tool_request(),
        "dummy-key",
        client_factory=sdk_tool_client_factory(provider, handle),
    )

    assert (response.usage.input_tokens, response.usage.output_tokens) == (1, 2)


def test_tool_turn_missing_usage_is_zero(sdk_environment, sdk_tool_client_factory):
    from lib.llm.providers import call_tool_turn

    def handle(request):
        payload = tool_response("openai", request.url.path, final=True)
        payload["usage"] = None
        return sdk_environment.http.Response(200, json=payload)

    response = call_tool_turn(
        "openai",
        tool_request(),
        "dummy-key",
        client_factory=sdk_tool_client_factory("openai", handle),
    )

    assert (response.usage.input_tokens, response.usage.output_tokens) == (0, 0)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_system_instruction_and_summary_after_tool_error_without_tool_definitions(
    sdk_environment, sdk_tool_client_factory, provider
):
    from lib.llm import tool_types as tools
    from lib.llm.providers import call_tool_turn

    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return sdk_environment.http.Response(
            200, json=tool_response(provider, request.url.path, final=True)
        )

    instruction = "主張ごとに path:開始行-終了行 の根拠を JSON で返す"
    summary = "ここまでで分かったことをまとめて"
    request = replace(
        tool_request(),
        tools=(),
        messages=(
            tools.SystemMessage(instruction),
            tools.UserMessage("調べて"),
            tools.AssistantMessage("", (tools.ToolCall("pending", "lookup_value", {"value": 1}),)),
            tools.ToolResultsMessage((tools.ToolResult("pending", "上限のため実行しない", True),)),
            tools.UserMessage(summary),
        ),
    )

    response = call_tool_turn(
        provider, request, "dummy-key", client_factory=sdk_tool_client_factory(provider, handle)
    )

    assert response.message.text == "確認完了"
    assert len(bodies) == 1
    body = bodies[0]
    assert not body.get("tools")
    if provider == "anthropic":
        assert instruction in json.dumps(body["system"], ensure_ascii=False)
        messages = body["messages"]
        assert messages[-1]["role"] == "user"
        content = messages[-1]["content"]
        assert summary in json.dumps(content, ensure_ascii=False)
        outputs = [
            block
            for message in messages
            if isinstance(message["content"], list)
            for block in message["content"]
            if block["type"] == "tool_result"
        ]
        assert [block["tool_use_id"] for block in outputs] == ["pending"]
    elif provider == "openai":
        assert body["messages"][0] == {"role": "system", "content": instruction}
        assert body["messages"][-1] == {"role": "user", "content": summary}
        outputs = [message for message in body["messages"] if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in outputs] == ["pending"]
    else:
        assert instruction in json.dumps(body["systemInstruction"], ensure_ascii=False)
        contents = body["contents"]
        assert contents[-1]["role"] == "user"
        assert summary in json.dumps(contents[-1]["parts"], ensure_ascii=False)
        outputs = [
            part["functionResponse"]
            for content in contents
            for part in content["parts"]
            if "functionResponse" in part
        ]
        assert [part["id"] for part in outputs] == ["pending"]
