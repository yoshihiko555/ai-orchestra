"""プロバイダ名から一回の API 呼び出しへ振り分ける。"""

from collections.abc import Callable
from typing import Any

from . import anthropic_provider, gemini_provider, openai_provider
from .tool_turn import call as call_tool_turn_adapter
from .tool_types import ToolTurnRequest, ToolTurnResponse
from .types import LLMRequest, LLMResponse


def call_provider(provider: str, request: LLMRequest, api_key: str) -> LLMResponse:
    adapters = {
        "anthropic": anthropic_provider,
        "openai": openai_provider,
        "gemini": gemini_provider,
    }
    return adapters[provider].call(request, api_key)


def call_tool_turn(
    provider: str,
    request: ToolTurnRequest,
    api_key: str,
    client_factory: Callable[..., Any] | None = None,
) -> ToolTurnResponse:
    return call_tool_turn_adapter(provider, request, api_key, client_factory)
