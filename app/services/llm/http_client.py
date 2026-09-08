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
    LlmProviderError,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderInvalidResponseError,
    ProviderResponseError,
    ProviderTimeoutError,
)


CONNECT_TIMEOUT_SECONDS = 10.0
TOTAL_TIMEOUT_SECONDS = 60.0
MAX_SSE_EVENT_BYTES = 96 * 1024
MAX_STREAM_OUTPUT_BYTES = 1024 * 1024
MAX_STREAM_TOOL_ARGUMENT_BYTES = 64 * 1024
MAX_STREAM_TOOL_CALLS = 4
MAX_ERROR_RESPONSE_BYTES = 256 * 1024
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

    def finish(self) -> tuple[str, ...]:
        """在 EOF 分发最后一个有界事件，兼容上游省略尾部空行。"""

        if not self._buffer:
            return ()
        raw_event = bytes(self._buffer)
        self._buffer.clear()
        self._require_event_size(raw_event)
        data = self._decode_event(raw_event)
        return (data,) if data is not None else ()

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


class _RawResponseCapture:
    """保存上游响应体前缀，供失败日志诊断且限制内存与日志体积。"""

    def __init__(self) -> None:
        self._content = bytearray()
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        remaining = MAX_ERROR_RESPONSE_BYTES - len(self._content)
        if remaining > 0:
            self._content.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.truncated = True

    @property
    def text(self) -> str:
        return bytes(self._content).decode("utf-8", errors="replace")


async def post_sse(
    url: str,
    api_key: str,
    payload: Mapping[str, Any],
    *,
    request_id: str,
    provider: str,
    model: str,
    on_data: Callable[[str], None],
    on_raw_response: Callable[[str, bool], None] | None = None,
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
    raw_response = _RawResponseCapture()
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
                        await _capture_stream_body(response, raw_response)
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
                        await _capture_stream_body(response, raw_response)
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
                        await _capture_stream_body(response, raw_response)
                        raise ProviderInvalidResponseError(
                            "Upstream service returned an invalid response"
                        )

                    decoder = _SseDecoder()
                    async for chunk in response.aiter_bytes():
                        raw_response.feed(chunk)
                        for data in decoder.feed(chunk):
                            on_data(data)
                    for data in decoder.finish():
                        on_data(data)
                    _log_stream_response(
                        response,
                        started_at,
                        clock,
                        provider,
                        model,
                        request_id,
                    )
                    if on_raw_response is not None:
                        on_raw_response(raw_response.text, raw_response.truncated)
                    return response.status_code
    except LlmProviderError as exc:
        exc.attach_raw_response(
            raw_response.text,
            truncated=raw_response.truncated,
        )
        raise
    except (TimeoutError, httpx.TimeoutException) as exc:
        log_model_http_error(
            request_id=request_id,
            provider=provider,
            model=model,
            error_type="timeout",
            duration_ms=_elapsed_ms(started_at, clock),
        )
        error = ProviderTimeoutError()
        if raw_response.text:
            error.attach_raw_response(
                raw_response.text,
                truncated=raw_response.truncated,
            )
        raise error from exc
    except httpx.RequestError as exc:
        log_model_http_error(
            request_id=request_id,
            provider=provider,
            model=model,
            error_type="connection",
            duration_ms=_elapsed_ms(started_at, clock),
        )
        error = ProviderConnectionError()
        if raw_response.text:
            error.attach_raw_response(
                raw_response.text,
                truncated=raw_response.truncated,
            )
        raise error from exc


async def _capture_stream_body(
    response: httpx.Response,
    capture: _RawResponseCapture,
) -> None:
    """读取错误响应的有界前缀；达到上限后由响应上下文负责关闭连接。"""

    async for chunk in response.aiter_bytes():
        capture.feed(chunk)
        if capture.truncated:
            break


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

    raw_response = _RawResponseCapture()
    raw_response.feed(response.content)

    if response.status_code in (401, 403):
        error = ProviderAuthenticationError(response.status_code)
        error.attach_raw_response(raw_response.text, truncated=raw_response.truncated)
        raise error
    if not response.is_success:
        error = ProviderResponseError(response.status_code)
        error.attach_raw_response(raw_response.text, truncated=raw_response.truncated)
        raise error

    try:
        return response.status_code, response.json()
    except ValueError as exc:
        error = ProviderInvalidResponseError()
        error.attach_raw_response(raw_response.text, truncated=raw_response.truncated)
        raise error from exc
