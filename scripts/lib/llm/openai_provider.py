"""OpenAI Responses API の一回の呼び出し。"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from .errors import classify_openai
from .sdk_logging import redact_sdk_logs
from .types import LLMRequest, LLMResponse


def _client_factory() -> Callable[..., Any]:
    from openai import OpenAI

    return OpenAI


def call(
    request: LLMRequest, api_key: str, client_factory: Callable[..., Any] | None = None
) -> LLMResponse:
    content: list[dict] = [{"type": "input_text", "text": request.prompt}]
    for attachment in request.attachments:
        data = base64.b64encode(attachment.data).decode("ascii")
        url = f"data:{attachment.mime_type};base64,{data}"
        if attachment.mime_type == "application/pdf":
            content.append({"type": "input_file", "filename": attachment.name, "file_data": url})
        else:
            content.append({"type": "input_image", "image_url": url, "detail": "auto"})
    options = {"tools": [{"type": "web_search"}]} if request.web_search else {}
    try:
        factory = client_factory if client_factory is not None else _client_factory()
        with (
            redact_sdk_logs(),
            factory(api_key=api_key, timeout=request.timeout_sec, max_retries=0) as client,
        ):
            response = client.responses.create(
                model=request.model,
                input=[{"role": "user", "content": content}],
                max_output_tokens=request.max_output_tokens,
                store=False,
                **options,
            )
            return LLMResponse(response.output_text)
    except Exception as exc:
        raise classify_openai(exc) from None
