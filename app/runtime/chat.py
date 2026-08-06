from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.services.aliyun_responses import (
    AliyunResponsesError,
    UpstreamAuthenticationError,
    UpstreamConfigurationError,
    UpstreamConnectionError,
    UpstreamInvalidResponseError,
    UpstreamResponseError,
    UpstreamTimeoutError,
    request_upstream_response,
    UPSTREAM_MODEL,
)


CHAT_PROVIDER = "Aliyun"
CHAT_MODEL = UPSTREAM_MODEL


@dataclass(frozen=True)
class ChatResult:
    upstream_status: int
    body: Any


class ChatErrorCode(str, Enum):
    INVALID_INPUT = "invalid_input"
    CONFIGURATION = "configuration"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    UPSTREAM = "upstream"
    INVALID_RESPONSE = "invalid_response"


class ChatRuntimeError(Exception):
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
    UpstreamConfigurationError: ChatErrorCode.CONFIGURATION,
    UpstreamTimeoutError: ChatErrorCode.TIMEOUT,
    UpstreamConnectionError: ChatErrorCode.CONNECTION,
    UpstreamAuthenticationError: ChatErrorCode.AUTHENTICATION,
    UpstreamResponseError: ChatErrorCode.UPSTREAM,
    UpstreamInvalidResponseError: ChatErrorCode.INVALID_RESPONSE,
}


async def run_chat(input_text: str) -> ChatResult:
    if not isinstance(input_text, str) or not input_text.strip():
        raise ChatRuntimeError(
            ChatErrorCode.INVALID_INPUT,
            "Input must not be blank",
        )

    try:
        status_code, body = await request_upstream_response(input_text)
    except AliyunResponsesError as exc:
        raise ChatRuntimeError(
            ERROR_CODES[type(exc)],
            exc.user_message,
            exc.status_code,
        ) from exc

    return ChatResult(upstream_status=status_code, body=body)
