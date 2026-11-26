"""Gemini GenerateContent API の一回の呼び出し。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .sdk_logging import redact_sdk_logs
from .types import LLMError, LLMRequest, LLMResponse


def _client_factory() -> Callable[..., Any]:
    from google import genai

    return genai.Client


def call(
    request: LLMRequest, api_key: str, client_factory: Callable[..., Any] | None = None
) -> LLMResponse:
    parts: list[dict] = [{"text": request.prompt}]
    for attachment in request.attachments:
        if attachment.mime_type == "image/gif":
            raise LLMError("unsupported_attachment", "Gemini は GIF 添付に対応していません。")
        parts.append({"inline_data": {"mime_type": attachment.mime_type, "data": attachment.data}})
    config: dict = {
        "max_output_tokens": request.max_output_tokens,
        "automatic_function_calling": {"disable": True},
    }
    if request.web_search:
        config["tools"] = [{"google_search": {}}]
    try:
        factory = client_factory if client_factory is not None else _client_factory()
        with (
            redact_sdk_logs(api_key),
            factory(
                api_key=api_key,
                http_options={
                    "timeout": max(1, int(request.timeout_sec * 1000)),
                    "retry_options": {"attempts": 1},
                },
            ) as client,
        ):
            response = client.models.generate_content(
                model=request.model,
                contents=[{"role": "user", "parts": parts}],
                config=config,
            )
            if not isinstance(response.text, str):
                raise LLMError("api_error", "API error: テキストの回答がありませんでした。")
            return LLMResponse(response.text)
    except LLMError:
        raise
    except Exception as exc:
        raise LLMError.from_exception(exc) from None
