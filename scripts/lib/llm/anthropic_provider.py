"""Anthropic Messages API の一回の呼び出し。"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from .sdk_logging import redact_sdk_logs
from .types import LLMError, LLMRequest, LLMResponse


def _client_factory() -> Callable[..., Any]:
    from anthropic import Anthropic

    return Anthropic


def call(
    request: LLMRequest, api_key: str, client_factory: Callable[..., Any] | None = None
) -> LLMResponse:
    content: list[dict] = [{"type": "text", "text": request.prompt}]
    for attachment in request.attachments:
        content.append(
            {
                "type": "document" if attachment.mime_type == "application/pdf" else "image",
                "source": {
                    "type": "base64",
                    "media_type": attachment.mime_type,
                    "data": base64.b64encode(attachment.data).decode("ascii"),
                },
            }
        )
    options = (
        {"tools": [{"type": "web_search_20250305", "name": "web_search"}]}
        if request.web_search
        else {}
    )
    try:
        factory = client_factory if client_factory is not None else _client_factory()
        with (
            redact_sdk_logs(api_key),
            factory(api_key=api_key, timeout=request.timeout_sec, max_retries=0) as client,
        ):
            response = client.messages.create(
                model=request.model,
                messages=[{"role": "user", "content": content}],
                max_tokens=request.max_output_tokens,
                **options,
            )
            text = "".join(block.text for block in response.content if block.type == "text")
            return LLMResponse(text)
    except Exception as exc:
        raise LLMError.from_exception(exc) from None
