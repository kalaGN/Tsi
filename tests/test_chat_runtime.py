import asyncio

import pytest

from app.runtime import chat
from app.runtime.chat import ChatErrorCode, ChatRuntimeError, run_chat
from app.services.aliyun_responses import (
    UpstreamAuthenticationError,
    UpstreamConfigurationError,
    UpstreamConnectionError,
    UpstreamInvalidResponseError,
    UpstreamResponseError,
    UpstreamTimeoutError,
)


def test_run_chat_returns_upstream_status_and_body(monkeypatch):
    body = {"id": "response-1", "output": [{"text": "hello"}]}

    async def fake_request(input_text: str):
        assert input_text == "hello"
        return 201, body

    monkeypatch.setattr(chat, "request_upstream_response", fake_request)

    result = asyncio.run(run_chat("hello"))

    assert result.upstream_status == 201
    assert result.body == body


def test_run_chat_rejects_blank_input_without_calling_service(monkeypatch):
    async def unexpected_request(input_text: str):
        raise AssertionError("service must not be called for blank input")

    monkeypatch.setattr(chat, "request_upstream_response", unexpected_request)

    with pytest.raises(ChatRuntimeError) as captured:
        asyncio.run(run_chat("   \n"))

    assert captured.value.code is ChatErrorCode.INVALID_INPUT
    assert captured.value.user_message == "Input must not be blank"
    assert captured.value.upstream_status is None


@pytest.mark.parametrize(
    ("service_error", "expected_code", "expected_message", "expected_status"),
    [
        (
            UpstreamConfigurationError(),
            ChatErrorCode.CONFIGURATION,
            "Upstream API key is not configured",
            None,
        ),
        (
            UpstreamTimeoutError(),
            ChatErrorCode.TIMEOUT,
            "Upstream request timed out",
            None,
        ),
        (
            UpstreamConnectionError(),
            ChatErrorCode.CONNECTION,
            "Unable to connect to upstream service",
            None,
        ),
        (
            UpstreamAuthenticationError(401),
            ChatErrorCode.AUTHENTICATION,
            "Upstream authentication failed",
            401,
        ),
        (
            UpstreamResponseError(429),
            ChatErrorCode.UPSTREAM,
            "Upstream service returned an error",
            429,
        ),
        (
            UpstreamInvalidResponseError(),
            ChatErrorCode.INVALID_RESPONSE,
            "Upstream service returned invalid JSON",
            None,
        ),
    ],
)
def test_run_chat_maps_service_errors(
    monkeypatch,
    service_error,
    expected_code,
    expected_message,
    expected_status,
):
    async def failing_request(input_text: str):
        raise service_error

    monkeypatch.setattr(chat, "request_upstream_response", failing_request)

    with pytest.raises(ChatRuntimeError) as captured:
        asyncio.run(run_chat("hello"))

    assert captured.value.code is expected_code
    assert captured.value.user_message == expected_message
    assert captured.value.upstream_status == expected_status
