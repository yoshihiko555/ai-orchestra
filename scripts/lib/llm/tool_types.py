"""道具の実装やSDKに依存しない、1往復の会話の型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, object]


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool


@dataclass(frozen=True)
class SystemMessage:
    text: str


@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class AssistantMessage:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    # プロバイダ名とJSON。SDKの型・可変オブジェクトを利用側へ公開しない。
    provider_state: tuple[str, str] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ToolResultsMessage:
    results: tuple[ToolResult, ...]


@dataclass(frozen=True)
class ToolTurnRequest:
    model: str
    messages: tuple[SystemMessage | UserMessage | AssistantMessage | ToolResultsMessage, ...]
    tools: tuple[ToolSpec, ...]
    timeout_sec: float
    max_output_tokens: int


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, int]:
        return {"input": self.input_tokens, "output": self.output_tokens, "total": self.total}


@dataclass(frozen=True)
class ToolTurnResponse:
    message: AssistantMessage
    usage: TokenUsage
    truncated: bool = False
