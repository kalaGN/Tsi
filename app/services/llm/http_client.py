"""不同模型 Provider 共用的安全异步 JSON 与 SSE 请求边界。"""

import asyncio
import re
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
MAX_SSE_EVENT_BYTES = 64 * 1024
MAX_STREAM_OUTPUT_BYTES = 1024 * 1024
MAX_STREAM_TOOL_ARGUMENT_BYTES = 8 * 1024
MAX_STREAM_TOOL_CALLS = 4
PROVIDER_TIMEOUT = httpx.Timeout(TOTAL_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
_TIMEOUT_LOG = {
    "connect_seconds": CONNECT_TIMEOUT_SECONDS,
    "total_seconds": TOTAL_TIMEOUT_SECONDS,
}


def _elapsed_ms(started_at: float, clock: Callable[[], float]) -> float:
    """把单调时钟增量统一转换为保留两位小数的毫秒值。"""

    return round((clock() - started_at) * 1000, 2)


class _SseDecoder:
    """在有界字节缓冲中切分 SSE 事件，并严格解码 UTF-8 data 字段。"""

    _EVENT_BOUNDARY = re.compile(br"(?:\r\n|\r|\n){2}")
    _LINE_BOUNDARY = re.compile(r"\r\n|\r|\n")

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> tuple[str, ...]:
        """接收任意网络分块并返回其中已经闭合的 data 事件。"""

        self._buffer.extend(chunk)
        events: list[str] = []
        while match := self._EVENT_BOUNDARY.search(self._buffer):
            raw_event = bytes(self._buffer[: match.start()])
            del self._buffer[: match.end()]
            self._require_event_size(raw_event)
            data = self._decode_event(raw_event)
            if data is not None:
                events.append(data)
        self._require_event_size(self._buffer)
        return tuple(events)

    def finish(self) -> None:
        """拒绝 EOF 时未以空行闭合的残余事件。"""

        if self._buffer:
            raise ProviderInvalidResponseError(
                "Upstream service returned an invalid response"
            )

    @staticmethod
    def _require_event_size(raw_event: bytes | bytearray) -> None:
        """限制单事件大小，避免异常上游无限扩张内存。"""

        if len(raw_event) > MAX_SSE_EVENT_BYTES:
            raise ProviderInvalidResponseError(
                "Upstream service returned an invalid response"
            )

    def _decode_event(self, raw_event: bytes) -> str | None:
        """按 SSE 规则拼接 data 行，忽略注释和其他字段。"""

        try:
            text = raw_event.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProviderInvalidResponseError(
                "Upstream service returned an invalid response"
            ) from exc

        data_lines: list[str] = []
        for line in self._LINE_BOUNDARY.split(text):
            if not line or line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if field != "data":
                continue
            if separator and value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
        return "\n".join(data_lines) if data_lines else None


async def post_sse(
    url: str,
    api_key: str,
    payload: Mapping[str, Any],
    *,
    request_id: str,
    provider: str,
    model: str,
    on_data: Callable[[str], None],
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """完整消费认证 SSE 响应，交付有界 data 事件并统一网络错误。"""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
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

    started_at = clock()
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(timeout=PROVIDER_TIMEOUT) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code in (401, 403):
                        _log_stream_response(
                            response,
                            started_at,
                            clock,
                            provider,
                            model,
                            request_id,
                        )
                        raise ProviderAuthenticationError(response.status_code)
                    if not response.is_success:
                        _log_stream_response(
                            response,
                            started_at,
                            clock,
                            provider,
                            model,
                            request_id,
                        )
                        raise ProviderResponseError(response.status_code)
                    content_type = response.headers.get("content-type", "")
                    if not content_type.lower().startswith("text/event-stream"):
                        raise ProviderInvalidResponseError(
                            "Upstream service returned an invalid response"
                        )

                    decoder = _SseDecoder()
                    async for chunk in response.aiter_bytes():
                        for data in decoder.feed(chunk):
                            on_data(data)
                    decoder.finish()
                    _log_stream_response(
                        response,
                        started_at,
                        clock,
                        provider,
                        model,
                        request_id,
                    )
                    return response.status_code
    except (TimeoutError, httpx.TimeoutException) as exc:
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


def _log_stream_response(
    response: httpx.Response,
    started_at: float,
    clock: Callable[[], float],
    provider: str,
    model: str,
    request_id: str,
) -> None:
    """为一次已收到的 SSE 响应记录单条状态与完整生命周期耗时。"""

    log_model_http_response(
        request_id=request_id,
        provider=provider,
        model=model,
        status_code=response.status_code,
        duration_ms=_elapsed_ms(started_at, clock),
        response_content_type=response.headers.get("content-type"),
    )


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
