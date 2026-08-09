import asyncio
import io
import json
import logging

import httpx
import pytest

from app.observability import model_logging
from app.runtime import chat
from app.runtime.chat import (
    ChatErrorCode,
    ChatRuntimeError,
    get_chat_runtime_info,
    run_chat,
    run_chat_messages,
)
from app.services.llm import http_client
from app.services.llm.contracts import (
    ChatMessage,
    ChatRole,
    ModelStep,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderInvalidResponseError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from app.services.llm.deepseek import DeepSeekChatProvider


class FakeProvider:
    name = "fake"
    model = "fake-model"
    api_key_configured = True

    def __init__(self, result=None, error=None, on_next=None):
        self.result = result
        self.error = error
        self.on_next = on_next
        self.received_inputs = []
        self.received_tools = []
        self.request_ids = []

    def create_turn(self, messages, tools, *, request_id):
        self.received_inputs.append(tuple(messages))
        self.received_tools.append(tuple(tools))
        self.request_ids.append(request_id)
        return FakeTurn(self)


class FakeTurn:
    def __init__(self, provider):
        self.provider = provider

    async def next(self, tool_results=()):
        if self.provider.on_next is not None:
            self.provider.on_next()
        if self.provider.error is not None:
            raise self.provider.error
        return self.provider.result


def test_run_chat_returns_normalized_provider_result():
    provider = FakeProvider(
        result=ModelStep(upstream_status=201, output_text="hello", tool_calls=())
    )

    result = asyncio.run(run_chat("hello", provider=provider))

    assert provider.received_inputs == [
        (ChatMessage(ChatRole.USER, "hello"),)
    ]
    assert result.output_text == "hello"
    assert result.provider == "fake"
    assert result.model == "fake-model"
    assert not hasattr(result, "raw_body")
    assert not hasattr(result, "upstream_status")


def test_run_chat_logs_input_and_output_around_provider_call(monkeypatch):
    call_order = []
    captured_requests = []
    captured_responses = []

    provider = FakeProvider(
        result=ModelStep(200, "secret-output", ()),
        on_next=lambda: call_order.append("provider"),
    )

    def capture_log(**fields):
        call_order.append("request_log")
        captured_requests.append(fields)

    def capture_response(**fields):
        call_order.append("response_log")
        captured_responses.append(fields)

    monkeypatch.setattr(chat, "new_request_id", lambda: "c" * 32)
    monkeypatch.setattr(chat, "log_model_request", capture_log)
    monkeypatch.setattr(chat, "log_model_response", capture_response)

    asyncio.run(run_chat("secret-input", provider=provider))

    assert call_order == ["request_log", "provider", "response_log"]
    assert captured_requests == [
        {
            "request_id": "c" * 32,
            "provider": "fake",
            "model": "fake-model",
            "input_chars": len("secret-input"),
            "input_text": "secret-input",
        }
    ]
    assert captured_responses == [
        {
            "request_id": "c" * 32,
            "provider": "fake",
            "model": "fake-model",
            "output_chars": len("secret-output"),
            "output_text": "secret-output",
        }
    ]


def test_run_chat_logs_only_request_when_provider_fails(monkeypatch):
    captured_requests = []
    captured_responses = []
    provider = FakeProvider(error=ProviderTimeoutError())
    monkeypatch.setattr(chat, "new_request_id", lambda: "d" * 32)
    monkeypatch.setattr(
        chat,
        "log_model_request",
        lambda **fields: captured_requests.append(fields),
    )
    monkeypatch.setattr(
        chat,
        "log_model_response",
        lambda **fields: captured_responses.append(fields),
    )

    with pytest.raises(ChatRuntimeError):
        asyncio.run(run_chat("hello", provider=provider))

    assert len(captured_requests) == 1
    assert captured_requests[0]["request_id"] == "d" * 32
    assert captured_responses == []


def test_multi_turn_runtime_logs_only_current_user_input(monkeypatch):
    captured_requests = []
    provider = FakeProvider(result=ModelStep(200, "answer-2", ()))
    monkeypatch.setattr(chat, "new_request_id", lambda: "e" * 32)
    monkeypatch.setattr(
        chat,
        "log_model_request",
        lambda **fields: captured_requests.append(fields),
    )
    monkeypatch.setattr(chat, "log_model_response", lambda **fields: None)
    messages = (
        ChatMessage(ChatRole.USER, "first"),
        ChatMessage(ChatRole.ASSISTANT, "answer-1"),
        ChatMessage(ChatRole.USER, "second"),
    )

    asyncio.run(run_chat_messages(messages, provider=provider))

    assert captured_requests[0]["input_text"] == "second"
    assert captured_requests[0]["input_chars"] == len("second")
    assert "first" not in repr(captured_requests)


def test_run_chat_rejects_blank_input_without_calling_provider():
    provider = FakeProvider(
        result=ModelStep(200, "should not be returned", ())
    )

    with pytest.raises(ChatRuntimeError) as captured:
        asyncio.run(run_chat("   \n", provider=provider))

    assert provider.received_inputs == []
    assert captured.value.code is ChatErrorCode.INVALID_INPUT
    assert captured.value.user_message == "Input must not be blank"
    assert captured.value.upstream_status is None


@pytest.mark.parametrize(
    ("provider_error", "expected_code", "expected_message", "expected_status"),
    [
        (
            ProviderConfigurationError("Upstream API key is not configured"),
            ChatErrorCode.CONFIGURATION,
            "Upstream API key is not configured",
            None,
        ),
        (
            ProviderConfigurationError("Unsupported LLM provider configuration"),
            ChatErrorCode.CONFIGURATION,
            "Unsupported LLM provider configuration",
            None,
        ),
        (
            ProviderTimeoutError(),
            ChatErrorCode.TIMEOUT,
            "Upstream request timed out",
            None,
        ),
        (
            ProviderConnectionError(),
            ChatErrorCode.CONNECTION,
            "Unable to connect to upstream service",
            None,
        ),
        (
            ProviderAuthenticationError(401),
            ChatErrorCode.AUTHENTICATION,
            "Upstream authentication failed",
            401,
        ),
        (
            ProviderResponseError(429),
            ChatErrorCode.UPSTREAM,
            "Upstream service returned an error",
            429,
        ),
        (
            ProviderInvalidResponseError(),
            ChatErrorCode.INVALID_RESPONSE,
            "Upstream service returned invalid JSON",
            None,
        ),
    ],
)
def test_run_chat_maps_provider_errors(
    provider_error,
    expected_code,
    expected_message,
    expected_status,
):
    provider = FakeProvider(error=provider_error)

    with pytest.raises(ChatRuntimeError) as captured:
        asyncio.run(run_chat("hello", provider=provider))

    assert captured.value.code is expected_code
    assert captured.value.user_message == expected_message
    assert captured.value.upstream_status == expected_status


def test_run_chat_maps_factory_configuration_error(monkeypatch):
    captured_requests = []
    captured_responses = []

    def fail_to_create_provider():
        raise ProviderConfigurationError("Unsupported LLM provider configuration")

    monkeypatch.setattr(chat, "create_provider", fail_to_create_provider)
    monkeypatch.setattr(
        chat,
        "log_model_request",
        lambda **fields: captured_requests.append(fields),
    )
    monkeypatch.setattr(
        chat,
        "log_model_response",
        lambda **fields: captured_responses.append(fields),
    )

    with pytest.raises(ChatRuntimeError) as captured:
        asyncio.run(run_chat("hello"))

    assert captured.value.code is ChatErrorCode.CONFIGURATION
    assert captured.value.user_message == "Unsupported LLM provider configuration"
    assert captured_requests == []
    assert captured_responses == []


def test_runtime_info_uses_selected_provider(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(chat, "create_provider", lambda: provider)

    info = get_chat_runtime_info()

    assert info.provider == "fake"
    assert info.model == "fake-model"
    assert info.api_key_configured is True


def test_runtime_info_maps_invalid_configuration(monkeypatch):
    def fail_to_create_provider():
        raise ProviderConfigurationError("Unsupported LLM provider configuration")

    monkeypatch.setattr(chat, "create_provider", fail_to_create_provider)

    with pytest.raises(ChatRuntimeError) as captured:
        get_chat_runtime_info()

    assert captured.value.code is ChatErrorCode.CONFIGURATION
    assert captured.value.user_message == "Unsupported LLM provider configuration"


def test_run_chat_emits_four_correlated_events_with_real_http_boundary(
    monkeypatch, tmp_path
):
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )
    )
    monkeypatch.setattr(
        http_client.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )

    stream = io.StringIO()
    logger = logging.getLogger(model_logging.LOGGER_NAME)
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    for handler in original_handlers:
        logger.removeHandler(handler)
    model_logging.configure_model_logging(stream=stream, log_path=tmp_path / "m.log")

    try:
        provider = DeepSeekChatProvider("test-only-key", "deepseek-v4-flash")
        result = asyncio.run(run_chat("hello", provider=provider))
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            logger.addHandler(handler)
        logger.setLevel(original_level)
        logger.propagate = original_propagate

    events = [json.loads(line) for line in stream.getvalue().splitlines()]

    assert result.output_text == "ok"
    assert [event["event"] for event in events] == [
        "llm_request",
        "llm_http_request",
        "llm_http_response",
        "llm_response",
    ]
    shared_id = events[0]["request_id"]
    assert len(shared_id) == 32
    for event in events:
        assert event["request_id"] == shared_id
        assert event["provider"] == "deepseek"
        assert event["model"] == "deepseek-v4-flash"


def test_run_chat_emits_correlated_events_for_complete_tool_loop(
    monkeypatch,
    tmp_path,
):
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "time-call",
                                        "type": "function",
                                        "function": {
                                            "name": "get_current_time",
                                            "arguments": '{"timezone":"UTC"}',
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "final"}}]},
        )

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        http_client.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )

    stream = io.StringIO()
    logger = logging.getLogger(model_logging.LOGGER_NAME)
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    for log_handler in original_handlers:
        logger.removeHandler(log_handler)
    model_logging.configure_model_logging(
        stream=stream,
        log_path=tmp_path / "tool.log",
    )

    try:
        result = asyncio.run(
            run_chat(
                "time?",
                provider=DeepSeekChatProvider("test-only-key"),
            )
        )
    finally:
        for log_handler in list(logger.handlers):
            logger.removeHandler(log_handler)
            log_handler.close()
        for log_handler in original_handlers:
            logger.addHandler(log_handler)
        logger.setLevel(original_level)
        logger.propagate = original_propagate

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert result.output_text == "final"
    assert [event["event"] for event in events] == [
        "llm_request",
        "llm_http_request",
        "llm_http_response",
        "llm_tool_call",
        "llm_tool_result",
        "llm_http_request",
        "llm_http_response",
        "llm_response",
    ]
    assert {event["request_id"] for event in events} == {events[0]["request_id"]}
    assert events[3]["call_id"] == events[4]["call_id"] == "time-call"
