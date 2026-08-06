"""阿里云兼容模式 Responses API 的异步 Provider 适配器。"""

import os
from typing import Any

import httpx


UPSTREAM_RESPONSES_URL = (
    "https://llm-h2k07hgnp4aylibi.cn-beijing.maas.aliyuncs.com/"
    "compatible-mode/v1/responses"
)
UPSTREAM_MODEL = "qwen3-max"
UPSTREAM_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class AliyunResponsesError(Exception):
    """Base class for safe errors raised by the Aliyun provider adapter."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.user_message = message
        self.status_code = status_code


class UpstreamConfigurationError(AliyunResponsesError):
    def __init__(self) -> None:
        super().__init__("Upstream API key is not configured")


class UpstreamTimeoutError(AliyunResponsesError):
    def __init__(self) -> None:
        super().__init__("Upstream request timed out")


class UpstreamConnectionError(AliyunResponsesError):
    def __init__(self) -> None:
        super().__init__("Unable to connect to upstream service")


class UpstreamAuthenticationError(AliyunResponsesError):
    def __init__(self, status_code: int) -> None:
        super().__init__("Upstream authentication failed", status_code)


class UpstreamResponseError(AliyunResponsesError):
    def __init__(self, status_code: int) -> None:
        super().__init__("Upstream service returned an error", status_code)


class UpstreamInvalidResponseError(AliyunResponsesError):
    def __init__(self) -> None:
        super().__init__("Upstream service returned invalid JSON")


async def request_upstream_response(input_text: str) -> tuple[int, Any]:
    """调用固定模型并返回成功响应；失败只抛出已脱敏异常。"""

    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key or not api_key.strip():
        raise UpstreamConfigurationError

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"model": UPSTREAM_MODEL, "input": input_text}

    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            response = await client.post(
                UPSTREAM_RESPONSES_URL,
                headers=headers,
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise UpstreamTimeoutError from exc
    except httpx.RequestError as exc:
        raise UpstreamConnectionError from exc

    if response.status_code in (401, 403):
        raise UpstreamAuthenticationError(response.status_code)
    if not response.is_success:
        # 不携带上游错误正文，避免响应中潜在的内部信息泄露给调用方。
        raise UpstreamResponseError(response.status_code)

    try:
        return response.status_code, response.json()
    except ValueError as exc:
        raise UpstreamInvalidResponseError from exc
