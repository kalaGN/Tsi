"""模型 Provider 的稳定契约和不泄漏上游细节的共享异常。"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol, Sequence

from tools.contracts import ToolCall, ToolDefinition, ToolResult


TextDeltaHandler = Callable[[str], None]
TextResetHandler = Callable[[], None]


class ChatRole(str, Enum):
    """当前文本对话支持的 Provider 中立角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class ChatMessage:
    """不携带 Provider 私有字段的单条文本消息。"""

    role: ChatRole
    content: str


@dataclass(frozen=True)
class ModelStep:
    """一次模型步骤的中立文本或工具调用结果。"""

    upstream_status: int
    output_text: str | None
    tool_calls: tuple[ToolCall, ...]


@dataclass(frozen=True)
class ProviderConfig:
    """环境解析结果；密钥不参与 repr，避免调试输出意外泄漏。"""

    provider: str
    model: str
    api_key: str = field(repr=False)

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key)


class LlmTurn(Protocol):
    """封装单个用户请求内的 Provider 私有续接状态。"""

    async def next(
        self,
        tool_results: Sequence[ToolResult] = (),
        *,
        on_text_delta: TextDeltaHandler | None = None,
    ) -> ModelStep:
        ...


class LlmProvider(Protocol):
    """Runtime 所依赖的有序消息和短生命周期 Turn 能力。"""

    name: str
    model: str

    @property
    def api_key_configured(self) -> bool:
        ...

    def create_turn(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition],
        *,
        request_id: str,
    ) -> LlmTurn:
        ...


class LlmProviderError(Exception):
    """所有可安全传递到 Runtime 的 Provider 错误基类。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.user_message = message
        self.status_code = status_code


class ProviderConfigurationError(LlmProviderError):
    pass


class ProviderInvalidRequestError(LlmProviderError):
    """内部消息序列违反 Provider 请求契约。"""

    def __init__(self) -> None:
        super().__init__("Conversation messages are invalid")


class ProviderTimeoutError(LlmProviderError):
    def __init__(self) -> None:
        super().__init__("Upstream request timed out")


class ProviderConnectionError(LlmProviderError):
    def __init__(self) -> None:
        super().__init__("Unable to connect to upstream service")


class ProviderAuthenticationError(LlmProviderError):
    def __init__(self, status_code: int) -> None:
        super().__init__("Upstream authentication failed", status_code)


class ProviderResponseError(LlmProviderError):
    def __init__(self, status_code: int) -> None:
        super().__init__("Upstream service returned an error", status_code)


class ProviderInvalidResponseError(LlmProviderError):
    def __init__(
        self,
        message: str = "Upstream service returned invalid JSON",
    ) -> None:
        super().__init__(message)


def validate_provider_messages(
    messages: Sequence[ChatMessage],
) -> tuple[ChatMessage, ...]:
    """验证可选首条 system 后是以 user 结束的交替对话。"""

    normalized = tuple(messages)
    if not normalized:
        raise ProviderInvalidRequestError()

    conversation_start = 0
    first_message = normalized[0]
    if isinstance(first_message, ChatMessage) and first_message.role is ChatRole.SYSTEM:
        if not _has_nonblank_content(first_message):
            raise ProviderInvalidRequestError()
        conversation_start = 1

    conversation = normalized[conversation_start:]
    if not conversation or len(conversation) % 2 == 0:
        raise ProviderInvalidRequestError()

    for index, message in enumerate(conversation):
        expected_role = ChatRole.USER if index % 2 == 0 else ChatRole.ASSISTANT
        if (
            not _has_nonblank_content(message)
            or message.role is not expected_role
        ):
            raise ProviderInvalidRequestError()
    return normalized


def _has_nonblank_content(message: object) -> bool:
    """集中校验中立消息形状，避免在非法对象上提前读取属性。"""

    return (
        isinstance(message, ChatMessage)
        and isinstance(message.content, str)
        and bool(message.content.strip())
    )
