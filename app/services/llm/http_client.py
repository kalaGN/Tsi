"""不同模型 Provider 共用的安全异步 JSON 请求边界。"""

from typing import Any, Mapping

import httpx

from app.services.llm.contracts import (
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderInvalidResponseError,
    ProviderResponseError,
    ProviderTimeoutError,
)


PROVIDER_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


async def post_json(
    url: str,
    api_key: str,
    payload: Mapping[str, Any],
) -> tuple[int, Any]:
    """发送认证 JSON 请求，并把外部故障转换为脱敏异常。"""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=PROVIDER_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        raise ProviderTimeoutError from exc
    except httpx.RequestError as exc:
        raise ProviderConnectionError from exc

    if response.status_code in (401, 403):
        raise ProviderAuthenticationError(response.status_code)
    if not response.is_success:
        # 上游错误体可能包含内部信息，只保留调用方需要的状态码。
        raise ProviderResponseError(response.status_code)

    try:
        return response.status_code, response.json()
    except ValueError as exc:
        raise ProviderInvalidResponseError from exc
