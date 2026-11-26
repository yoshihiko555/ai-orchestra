"""SDK 境界の失敗を、実 API に接続せず共通エラーへ変換する。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from lib.llm import anthropic_provider, gemini_provider, openai_provider
from lib.llm.types import Attachment, LLMError, LLMRequest


class APITimeoutError(Exception):
    """Anthropic / OpenAI の公開例外名を再現する SDK ダブル。"""


class TimeoutException(Exception):
    """Google SDK の HTTP transport が使う例外階層のダブル。"""


class ReadTimeout(TimeoutException):
    """具体的な transport timeout。"""


class APIError(Exception):
    """API 失敗のダブル。詳細な例外文を外へ出さないことを観測する。"""


@pytest.fixture
def llm_request() -> LLMRequest:
    return LLMRequest("test-model", "質問", (), False, 17, 123)


@pytest.mark.parametrize(
    ("provider", "resource", "method", "error", "category"),
    [
        (anthropic_provider, "messages", "create", APITimeoutError, "timeout"),
        (openai_provider, "responses", "create", APITimeoutError, "timeout"),
        (gemini_provider, "models", "generate_content", ReadTimeout, "timeout"),
        (anthropic_provider, "messages", "create", APIError, "api_error"),
        (openai_provider, "responses", "create", APIError, "api_error"),
        (gemini_provider, "models", "generate_content", APIError, "api_error"),
    ],
)
def test_sdk_failures_are_classified_without_secret_and_client_is_released(
    llm_request, provider, resource, method, error, category, capsys, caplog
) -> None:
    operation = Mock(side_effect=error("dummy-private-credentialmarker"))
    client = MagicMock(spec=[resource, "__enter__", "__exit__"])
    setattr(client, resource, SimpleNamespace(**{method: operation}))
    client.__enter__.return_value = client
    factory = Mock(return_value=client)

    with pytest.raises(LLMError) as caught:
        provider.call(llm_request, "dummy-private-credentialmarker", client_factory=factory)

    assert caught.value.category == category
    operation.assert_called_once()
    client.__exit__.assert_called_once()
    captured = capsys.readouterr()
    for output in (str(caught.value), captured.out, captured.err, caplog.text):
        assert "dummy-private" not in output
        assert "credentialmarker" not in output


def test_anthropic_extracts_text_among_server_tool_blocks(llm_request) -> None:
    client = MagicMock()
    client.__enter__.return_value = client
    client.messages.create.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(type="server_tool_use"),
            SimpleNamespace(type="text", text="根拠"),
            SimpleNamespace(type="web_search_tool_result"),
            SimpleNamespace(type="text", text="と結論"),
        ]
    )

    response = anthropic_provider.call(
        llm_request, "dummy-key", client_factory=Mock(return_value=client)
    )

    assert response.text == "根拠と結論"
    client.messages.create.assert_called_once()
    client.__exit__.assert_called_once()


def test_gemini_gif_is_rejected_before_client_creation(llm_request) -> None:
    from dataclasses import replace

    request = replace(llm_request, attachments=(Attachment("image.gif", "image/gif", b"GIF89a"),))
    factory = Mock()

    with pytest.raises(LLMError) as caught:
        gemini_provider.call(request, "dummy-key", client_factory=factory)

    assert caught.value.category == "unsupported_attachment"
    factory.assert_not_called()
