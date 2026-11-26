"""プロバイダ名から一回の API 呼び出しへ振り分ける。"""

from . import anthropic_provider, gemini_provider, openai_provider
from .types import LLMRequest, LLMResponse


def call_provider(provider: str, request: LLMRequest, api_key: str) -> LLMResponse:
    adapters = {
        "anthropic": anthropic_provider,
        "openai": openai_provider,
        "gemini": gemini_provider,
    }
    return adapters[provider].call(request, api_key)
