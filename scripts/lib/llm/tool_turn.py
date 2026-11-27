"""共通の会話をany-llmへ変換し、道具を実行せず1往復だけ返す。"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .errors import classify_anthropic, classify_gemini, classify_openai
from .sdk_logging import redact_sdk_logs
from .tool_types import (
    AssistantMessage,
    SystemMessage,
    TokenUsage,
    ToolCall,
    ToolResultsMessage,
    ToolTurnRequest,
    ToolTurnResponse,
    UserMessage,
)
from .types import LLMError


def _client_factory() -> Callable[..., Any]:
    from any_llm import AnyLLM

    return AnyLLM.create


@contextmanager
def _client(
    provider: str, request: ToolTurnRequest, api_key: str, factory: Callable[..., Any]
) -> Iterator[Any]:
    from any_llm.utils.aio import run_async_in_sync

    # クライアントの生成からcloseまで、診断値を文字列化する前に保護する。
    with redact_sdk_logs():
        instance = factory(
            provider,
            api_key=api_key,
            timeout=request.timeout_sec,
            max_retries=0,
            unified_exceptions=False,
        )
        try:
            yield instance
        finally:
            if provider == "gemini":
                try:
                    run_async_in_sync(instance.client.aio.aclose())
                finally:
                    instance.client.close()
            else:
                run_async_in_sync(instance.client.close())


def _messages(provider: str, request: ToolTurnRequest) -> list[dict]:
    messages: list[dict] = []
    calls: dict[str, ToolCall] = {}
    for message in request.messages:
        if isinstance(message, SystemMessage):
            messages.append({"role": "system", "content": message.text})
        elif isinstance(message, UserMessage):
            messages.append({"role": "user", "content": message.text})
        elif isinstance(message, AssistantMessage):
            raw: dict = {}
            if message.provider_state is not None:
                origin, payload = message.provider_state
                if origin != provider:
                    raise LLMError("api_error", "会話の状態は同じプロバイダへ渡してください。")
                raw = json.loads(payload)
            original_calls = {call["id"]: call for call in raw.get("tool_calls", [])}
            raw.update(role="assistant", content=message.text or None)
            raw["tool_calls"] = [
                {
                    **original_calls.get(call.call_id, {}),
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in message.tool_calls
            ]
            messages.append(raw)
            calls = {call.call_id: call for call in message.tool_calls}
        elif isinstance(message, ToolResultsMessage):
            for result in message.results:
                if result.call_id not in calls:
                    raise LLMError("api_error", "道具の結果に対応する呼び出しがありません。")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.call_id,
                        "name": calls[result.call_id].name,
                        "content": json.dumps({"error": result.content})
                        if result.is_error
                        else result.content,
                    }
                )
        else:
            raise LLMError("api_error", "未対応の会話メッセージです。")
    return messages


def call(
    provider: str,
    request: ToolTurnRequest,
    api_key: str,
    client_factory: Callable[..., Any] | None = None,
) -> ToolTurnResponse:
    classifiers = {
        "anthropic": classify_anthropic,
        "openai": classify_openai,
        "gemini": classify_gemini,
    }
    classify = classifiers[provider]
    try:
        messages = _messages(provider, request)
        factory = client_factory if client_factory is not None else _client_factory()
        options: dict = {}
        if provider == "gemini":
            options["automatic_function_calling"] = {"disable": True}
        elif provider == "openai":
            options["store"] = False
        with _client(provider, request, api_key, factory) as client:
            response = client.completion(
                model=request.model,
                messages=messages,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                        },
                    }
                    for tool in request.tools
                ],
                max_tokens=request.max_output_tokens,
                **options,
            )
            message = response.choices[0].message
            calls = []
            for item in message.tool_calls or ():
                arguments = json.loads(item.function.arguments)
                if not isinstance(arguments, dict):
                    raise LLMError("api_error", "道具の引数はJSONのオブジェクトが必要です。")
                calls.append(ToolCall(item.id, item.function.name, arguments))
            return ToolTurnResponse(
                AssistantMessage(
                    text=message.content or "",
                    tool_calls=tuple(calls),
                    provider_state=(provider, message.model_dump_json(exclude_none=True)),
                ),
                usage=TokenUsage(
                    _token_count(response.usage, "prompt_tokens"),
                    _token_count(response.usage, "completion_tokens"),
                ),
            )
    except LLMError:
        raise
    except Exception as exc:
        raise classify(exc) from None


def _token_count(usage: object, field: str) -> int:
    value = getattr(usage, field, None)
    return value if type(value) is int and value >= 0 else 0
