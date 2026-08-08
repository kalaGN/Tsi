"""不同模型 Provider 共用的安全异步 JSON 请求边界。"""

import time
from typing import Any, Callable, Mapping

import httpx

from app.observability.model_logging import (
    log_model_http_error,
    log_model_http_request,
    log_model_http_response,
)
from app.services.llm.contracts import (
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderInvalidResponseError,
    ProviderResponseError,
    ProviderTimeoutError,
)


CONNECT_TIMEOUT_SECONDS = 10.0
TOTAL_TIMEOUT_SECONDS = 60.0
PROVIDER_TIMEOUT = httpx.Timeout(TOTAL_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
_TIMEOUT_LOG = {
    "connect_seconds": CONNECT_TIMEOUT_SECONDS,
    "total_seconds": TOTAL_TIMEOUT_SECONDS,
}


def _elapsed_ms(started_at: float, clock: Callable[[], float]) -> float:
    """把单调时钟增量统一转换为保留两位小数的毫秒值。"""

    return round((clock() - started_at) * 1000, 2)


async def post_json(
    url: str,
    api_key: str,
    payload: Mapping[str, Any],
    *,
    request_id: str,
    provider: str,
    model: str,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, Any]:
    """发送认证 JSON 请求，在真实 I/O 边界旁路记录事件并转换外部故障。"""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    log_model_http_request(
        request_id=request_id,
        provider=provider,
        model=model,
        method="POST",
        url=url,
        request_body=payload,
        timeout=_TIMEOUT_LOG,
    )

    # 耗时从创建客户端前起算，收到响应或网络异常时结束。
    started_at = clock()
    try:
        async with httpx.AsyncClient(timeout=PROVIDER_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        log_model_http_error(
            request_id=request_id,
            provider=provider,
            model=model,
            error_type="timeout",
            duration_ms=_elapsed_ms(started_at, clock),
        )
        raise ProviderTimeoutError from exc
    except httpx.RequestError as exc:
        log_model_http_error(
            request_id=request_id,
            provider=provider,
            model=model,
            error_type="connection",
            duration_ms=_elapsed_ms(started_at, clock),
        )
        raise ProviderConnectionError from exc

    # 收到任意 HTTP 响应后立即记录，再执行状态映射和 JSON 解析。
    log_model_http_response(
        request_id=request_id,
        provider=provider,
        model=model,
        status_code=response.status_code,
        duration_ms=_elapsed_ms(started_at, clock),
        response_content_type=response.headers.get("content-type"),
    )

    if response.status_code in (401, 403):
        raise ProviderAuthenticationError(response.status_code)
    if not response.is_success:
        # 上游错误体可能包含内部信息，只保留调用方需要的状态码。
        raise ProviderResponseError(response.status_code)

    try:
        return response.status_code, response.json()
    except ValueError as exc:
        raise ProviderInvalidResponseError from exc
