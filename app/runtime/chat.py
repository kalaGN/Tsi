"""HTTP 与 TUI 共享的模型调用和界面无关错误语义。"""

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from app.observability.model_logging import (
    log_model_request,
    log_model_response,
    new_request_id,
)
from app.runtime.tool_loop import (
    DEFAULT_TOOL_LOOP_LIMITS,
    ToolLoopLimitError,
    ToolLoopLimits,
    run_tool_loop,
)
from app.services.llm.contracts import (
    ChatMessage,
    ChatRole,
    LlmProvider,
    LlmProviderError,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderInvalidResponseError,
    ProviderInvalidRequestError,
    ProviderResponseError,
    ProviderTimeoutError,
    TextDeltaHandler,
    TextResetHandler,
)
from app.services.llm.factory import create_provider
from tools import (
    ToolApprovalHandler,
    ToolRegistry,
    ToolResultHandler,
    create_default_registry,
)


@dataclass(frozen=True)
class ChatResult:
    """供不同交互界面使用的 Provider 无关文本结果。"""

    output_text: str
    provider: str
    model: str


@dataclass(frozen=True)
class ChatRuntimeInfo:
    """TUI 可安全展示的当前模型配置信息。"""

    provider: str
    model: str
    api_key_configured: bool


class ChatErrorCode(str, Enum):
    INVALID_INPUT = "invalid_input"
    CONFIGURATION = "configuration"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    UPSTREAM = "upstream"
    INVALID_RESPONSE = "invalid_response"
    STORAGE = "storage"
    TOOL_LIMIT = "tool_limit"


class ChatRuntimeError(Exception):
    """可安全传递到 HTTP 或 TUI 边界的用例错误。"""

    def __init__(
        self,
        code: ChatErrorCode,
        user_message: str,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message
        self.upstream_status = upstream_status


ERROR_CODES = {
    ProviderInvalidRequestError: ChatErrorCode.INVALID_INPUT,
    ProviderConfigurationError: ChatErrorCode.CONFIGURATION,
    ProviderTimeoutError: ChatErrorCode.TIMEOUT,
    ProviderConnectionError: ChatErrorCode.CONNECTION,
    ProviderAuthenticationError: ChatErrorCode.AUTHENTICATION,
    ProviderResponseError: ChatErrorCode.UPSTREAM,
    ProviderInvalidResponseError: ChatErrorCode.INVALID_RESPONSE,
}


async def run_chat(
    input_text: str,
    provider: LlmProvider | None = None,
    registry: ToolRegistry | None = None,
    *,
    on_text_delta: TextDeltaHandler | None = None,
    on_text_reset: TextResetHandler | None = None,
    on_tool_approval: ToolApprovalHandler | None = None,
    on_tool_result: ToolResultHandler | None = None,
    tool_loop_limits: ToolLoopLimits = DEFAULT_TOOL_LOOP_LIMITS,
) -> ChatResult:
    """校验输入、调用所选 Provider，并统一外部异常语义。"""

    if not isinstance(input_text, str) or not input_text.strip():
        raise ChatRuntimeError(
            ChatErrorCode.INVALID_INPUT,
            "Input must not be blank",
        )

    return await run_chat_messages(
        (ChatMessage(ChatRole.USER, input_text),),
        provider=provider,
        registry=registry,
        on_text_delta=on_text_delta,
        on_text_reset=on_text_reset,
        on_tool_approval=on_tool_approval,
        on_tool_result=on_tool_result,
        tool_loop_limits=tool_loop_limits,
    )


async def run_chat_messages(
    messages: Sequence[ChatMessage],
    provider: LlmProvider | None = None,
    registry: ToolRegistry | None = None,
    *,
    system_prompt: str | None = None,
    on_text_delta: TextDeltaHandler | None = None,
    on_text_reset: TextResetHandler | None = None,
    on_tool_approval: ToolApprovalHandler | None = None,
    on_tool_result: ToolResultHandler | None = None,
    tool_loop_limits: ToolLoopLimits = DEFAULT_TOOL_LOOP_LIMITS,
) -> ChatResult:
    """调用有序对话，可选 system 不改变当前 user 输入日志。"""

    conversation = tuple(messages)
    current_input = conversation[-1].content if conversation else ""
    provider_messages = conversation
    if system_prompt is not None:
        # 系统提示词只属于本次请求，不回写调用方维护的会话历史。
        provider_messages = (
            ChatMessage(ChatRole.SYSTEM, system_prompt),
        ) + conversation
    try:
        active_provider = create_provider() if provider is None else provider
        active_registry = create_default_registry() if registry is None else registry
        request_id = new_request_id()
        provider_name = active_provider.name
        model = active_provider.model
        log_model_request(
            request_id=request_id,
            provider=provider_name,
            model=model,
            input_chars=len(current_input),
            input_text=current_input,
        )
        turn = active_provider.create_turn(
            provider_messages,
            active_registry.definitions,
            request_id=request_id,
        )
        step = await run_tool_loop(
            turn,
            active_registry,
            request_id=request_id,
            on_text_delta=on_text_delta,
            on_text_reset=on_text_reset,
            on_tool_approval=on_tool_approval,
            on_tool_result=on_tool_result,
            limits=tool_loop_limits,
        )
        output_text = step.output_text
        if output_text is None:
            raise ProviderInvalidResponseError(
                "Upstream service returned an invalid response"
            )
        log_model_response(
            request_id=request_id,
            provider=provider_name,
            model=model,
            output_chars=len(output_text),
            output_text=output_text,
        )
    except ToolLoopLimitError as exc:
        raise ChatRuntimeError(
            ChatErrorCode.TOOL_LIMIT,
            "Tool call limit exceeded",
        ) from exc
    except LlmProviderError as exc:
        raise _runtime_error(exc) from exc

    return ChatResult(
        output_text=output_text,
        provider=provider_name,
        model=model,
    )


def get_chat_runtime_info() -> ChatRuntimeInfo:
    """读取与实际调用相同的 Provider 配置，不暴露密钥内容。"""

    try:
        provider = create_provider()
    except LlmProviderError as exc:
        raise _runtime_error(exc) from exc

    return ChatRuntimeInfo(
        provider=provider.name,
        model=provider.model,
        api_key_configured=provider.api_key_configured,
    )


def _runtime_error(error: LlmProviderError) -> ChatRuntimeError:
    """集中转换 Provider 错误，避免各交互边界重复判断。"""

    for error_type, code in ERROR_CODES.items():
        if isinstance(error, error_type):
            return ChatRuntimeError(code, error.user_message, error.status_code)
    return ChatRuntimeError(
        ChatErrorCode.UPSTREAM,
        "Upstream service returned an error",
        error.status_code,
    )
