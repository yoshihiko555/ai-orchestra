"""プロバイダ間で共有するリクエスト・応答・エラー。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Attachment:
    name: str
    mime_type: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class LLMRequest:
    model: str
    prompt: str
    attachments: tuple[Attachment, ...]
    web_search: bool
    timeout_sec: float
    max_output_tokens: int


@dataclass(frozen=True)
class LLMResponse:
    text: str


class LLMError(Exception):
    _CATEGORY_MESSAGES = {
        "quota_exhausted": "API の利用枠または残高を使い切りました。請求と利用枠を確認してください。",
        "rate_limited": "API のレート制限が解消しませんでした。時間を置いて再実行してください。",
        "auth_error": "API キーが無効か、権限がありません。orchex auth set で設定し直してください。",
    }

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category

    @classmethod
    def for_category(cls, category: str) -> LLMError:
        return cls(category, cls._CATEGORY_MESSAGES[category])

    @classmethod
    def from_exception(cls, exc: Exception) -> LLMError:
        timeout = isinstance(exc, TimeoutError) or any(
            base.__name__ in {"APITimeoutError", "TimeoutException"} for base in type(exc).__mro__
        )
        if timeout:
            return cls("timeout", "API 呼び出しがタイムアウトしました。")
        # SDK 例外は request・header・応答本文を含み得るため、文字列化しない。
        return cls("api_error", "API error: API リクエストに失敗しました。")
