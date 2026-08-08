import asyncio
import io
import json
import logging

import httpx
import pytest

from app.observability import model_logging
from app.services.llm.aliyun import (
    ALIYUN_RESPONSES_URL,
    AliyunResponsesProvider,
)
from app.services.llm.contracts import (
    ChatMessage,
    ChatRole,
    LlmProviderError,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderInvalidResponseError,
    ProviderInvalidRequestError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from app.services.llm.deepseek import (
    DEEPSEEK_CHAT_COMPLETIONS_URL,
    DeepSeekChatProvider,
)
from app.services.llm.factory import create_provider, resolve_provider_config
from app.services.llm import http_client
from app.services.llm.http_client import post_json


FAKE_API_KEY = "test-only-provider-key"
REQUEST_ID = "0123456789abcdef0123456789abcdef"


def user_messages(content: str = "hello") -> tuple[ChatMessage, ...]:
    return (ChatMessage(ChatRole.USER, content),)


def install_transport(monkeypatch, handler):
    """让 Provider 使用内存 Transport，确保测试不会访问真实模型。"""

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def create_client(**kwargs):
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(http_client.httpx, "AsyncClient", create_client)


def test_factory_defaults_to_deepseek_without_provider_setting():
    config = resolve_provider_config({"DEEPSEEK_API_KEY": FAKE_API_KEY})

    assert config.provider == "deepseek"
    assert config.model == "deepseek-v4-flash"
    assert config.api_key_configured is True
    assert FAKE_API_KEY not in repr(config)


def test_factory_normalizes_deepseek_and_model_override():
    config = resolve_provider_config(
        {
            "LLM_PROVIDER": "  DeepSeek ",
            "DEEPSEEK_API_KEY": FAKE_API_KEY,
            "DEEPSEEK_MODEL": " deepseek-v4-pro ",
        }
    )

    assert config.provider == "deepseek"
    assert config.model == "deepseek-v4-pro"
    assert config.api_key_configured is True


def test_factory_applies_aliyun_model_override():
    config = resolve_provider_config(
        {
            "LLM_PROVIDER": "aliyun",
            "DASHSCOPE_API_KEY": FAKE_API_KEY,
            "ALIYUN_MODEL": " custom-qwen ",
        }
    )

    assert config.provider == "aliyun"
    assert config.model == "custom-qwen"


@pytest.mark.parametrize("provider", ["", "   ", "unknown"])
def test_factory_rejects_blank_or_unknown_provider(provider):
    with pytest.raises(ProviderConfigurationError) as captured:
        resolve_provider_config({"LLM_PROVIDER": provider})

    assert captured.value.user_message == "Unsupported LLM provider configuration"


@pytest.mark.parametrize(
    ("environ", "expected_model"),
    [
        (
            {"LLM_PROVIDER": "aliyun", "ALIYUN_MODEL": "  "},
            "qwen3-max",
        ),
        (
            {"LLM_PROVIDER": "deepseek", "DEEPSEEK_MODEL": "  "},
            "deepseek-v4-flash",
        ),
    ],
)
def test_factory_uses_default_for_blank_model(environ, expected_model):
    assert resolve_provider_config(environ).model == expected_model


def test_factory_creates_selected_provider():
    aliyun = create_provider(
        {
            "LLM_PROVIDER": "aliyun",
            "DASHSCOPE_API_KEY": FAKE_API_KEY,
        }
    )
    deepseek = create_provider(
        {
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": FAKE_API_KEY,
        }
    )

    assert isinstance(aliyun, AliyunResponsesProvider)
    assert isinstance(deepseek, DeepSeekChatProvider)


@pytest.mark.parametrize(
    ("body", "expected_text"),
    [
        ({"output_text": "top-level"}, "top-level"),
        ({"output": [{"text": "first"}, {"text": "second"}]}, "first\nsecond"),
        (
            {
                "output": [
                    {"content": [{"text": "first"}, {"type": "metadata"}]},
                    {"content": [{"text": "second"}]},
                ]
            },
            "first\nsecond",
        ),
    ],
)
def test_aliyun_sends_expected_request_and_extracts_text(
    monkeypatch,
    body,
    expected_text,
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == ALIYUN_RESPONSES_URL
        assert request.headers["Authorization"] == f"Bearer {FAKE_API_KEY}"
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["Accept"] == "application/json"
        assert request.content == b'{"model":"custom-qwen","input":"hello"}'
        return httpx.Response(200, json=body)

    install_transport(monkeypatch, handler)
    provider = AliyunResponsesProvider(FAKE_API_KEY, "custom-qwen")

    result = asyncio.run(provider.generate(user_messages(), request_id=REQUEST_ID))

    assert result.upstream_status == 200
    assert result.raw_body == body
    assert result.output_text == expected_text


def test_deepseek_sends_expected_request_and_extracts_text(monkeypatch):
    body = {
        "id": "chat-1",
        "choices": [{"message": {"role": "assistant", "content": "你好"}}],
        "usage": {"total_tokens": 3},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == DEEPSEEK_CHAT_COMPLETIONS_URL
        assert request.headers["Authorization"] == f"Bearer {FAKE_API_KEY}"
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["Accept"] == "application/json"
        assert request.content == (
            b'{"model":"deepseek-v4-pro","messages":'
            b'[{"role":"user","content":"\xe4\xbd\xa0\xe5\xa5\xbd"}],"stream":false}'
        )
        return httpx.Response(200, json=body)

    install_transport(monkeypatch, handler)
    provider = DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-pro")

    result = asyncio.run(provider.generate(user_messages("你好"), request_id=REQUEST_ID))

    assert result.upstream_status == 200
    assert result.raw_body == body
    assert result.output_text == "你好"


@pytest.mark.parametrize(
    ("provider", "expected_field"),
    [
        (AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"), "input"),
        (
            DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
            "messages",
        ),
    ],
)
def test_provider_sends_ordered_multi_turn_messages(
    monkeypatch,
    provider,
    expected_field,
):
    history = (
        ChatMessage(ChatRole.USER, "first"),
        ChatMessage(ChatRole.ASSISTANT, "answer"),
        ChatMessage(ChatRole.USER, "second"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload[expected_field] == [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ]
        if provider.name == "aliyun":
            return httpx.Response(200, json={"output_text": "ok"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    install_transport(monkeypatch, handler)

    assert asyncio.run(provider.generate(history, request_id=REQUEST_ID)).output_text == "ok"


@pytest.mark.parametrize(
    "messages",
    [
        (),
        (ChatMessage(ChatRole.USER, ""),),
        (ChatMessage(ChatRole.ASSISTANT, "orphan"),),
        (
            ChatMessage(ChatRole.USER, "first"),
            ChatMessage(ChatRole.USER, "second"),
            ChatMessage(ChatRole.USER, "third"),
        ),
    ],
)
def test_provider_rejects_invalid_message_sequence_without_network(
    monkeypatch,
    messages,
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid messages must not reach network")

    install_transport(monkeypatch, handler)
    provider = DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash")

    with pytest.raises(ProviderInvalidRequestError) as captured:
        asyncio.run(provider.generate(messages, request_id=REQUEST_ID))

    assert str(captured.value) == "Conversation messages are invalid"


@pytest.mark.parametrize(
    "provider",
    [
        AliyunResponsesProvider("", "qwen3-max"),
        DeepSeekChatProvider("   ", "deepseek-v4-flash"),
    ],
)
def test_provider_requires_key_before_network_call(monkeypatch, provider):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called without a key")

    install_transport(monkeypatch, handler)

    with pytest.raises(ProviderConfigurationError) as captured:
        asyncio.run(provider.generate(user_messages(), request_id=REQUEST_ID))

    assert captured.value.user_message == "Upstream API key is not configured"


@pytest.mark.parametrize(
    "provider",
    [
        AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"),
        DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
    ],
)
def test_provider_maps_timeout(monkeypatch, provider):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret timeout detail", request=request)

    install_transport(monkeypatch, handler)

    with pytest.raises(ProviderTimeoutError) as captured:
        asyncio.run(provider.generate(user_messages(), request_id=REQUEST_ID))

    assert captured.value.user_message == "Upstream request timed out"
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize(
    "provider",
    [
        AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"),
        DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
    ],
)
def test_provider_maps_connection_error(monkeypatch, provider):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret connection detail", request=request)

    install_transport(monkeypatch, handler)

    with pytest.raises(ProviderConnectionError) as captured:
        asyncio.run(provider.generate(user_messages(), request_id=REQUEST_ID))

    assert captured.value.user_message == "Unable to connect to upstream service"
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize("status_code", [401, 403])
def test_provider_maps_authentication_error(monkeypatch, status_code):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "secret upstream body"})

    install_transport(monkeypatch, handler)
    provider = DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash")

    with pytest.raises(ProviderAuthenticationError) as captured:
        asyncio.run(provider.generate(user_messages(), request_id=REQUEST_ID))

    assert captured.value.status_code == status_code
    assert captured.value.user_message == "Upstream authentication failed"
    assert "secret" not in str(captured.value)
    assert FAKE_API_KEY not in str(captured.value)


@pytest.mark.parametrize("status_code", [302, 400, 402, 422, 429, 500, 503])
def test_deepseek_preserves_other_error_status(monkeypatch, status_code):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "secret upstream body"})

    install_transport(monkeypatch, handler)
    provider = DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash")

    with pytest.raises(ProviderResponseError) as captured:
        asyncio.run(provider.generate(user_messages(), request_id=REQUEST_ID))

    assert captured.value.status_code == status_code
    assert captured.value.user_message == "Upstream service returned an error"
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize(
    "provider",
    [
        AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"),
        DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
    ],
)
def test_provider_rejects_non_json_success(monkeypatch, provider):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    install_transport(monkeypatch, handler)

    with pytest.raises(ProviderInvalidResponseError) as captured:
        asyncio.run(provider.generate(user_messages(), request_id=REQUEST_ID))

    assert captured.value.user_message == "Upstream service returned invalid JSON"


@pytest.mark.parametrize(
    ("provider", "body"),
    [
        (AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"), {"output": []}),
        (DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"), {}),
        (
            DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
            {"choices": []},
        ),
        (
            DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
            {"choices": [{"message": {"content": None}}]},
        ),
        (
            DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
            {"choices": [{"message": {"content": ""}}]},
        ),
    ],
)
def test_provider_rejects_success_without_text(monkeypatch, provider, body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    install_transport(monkeypatch, handler)

    with pytest.raises(ProviderInvalidResponseError) as captured:
        asyncio.run(provider.generate(user_messages(), request_id=REQUEST_ID))

    assert captured.value.user_message == "Upstream service returned an invalid response"


@pytest.fixture
def captured_model_events(tmp_path):
    """配置模型日志到内存流，返回解析事件并在测试后还原 Logger。"""

    stream = io.StringIO()
    log_path = tmp_path / "model-calls.log"
    logger = logging.getLogger(model_logging.LOGGER_NAME)
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    for handler in original_handlers:
        logger.removeHandler(handler)
    model_logging.configure_model_logging(stream=stream, log_path=log_path)

    def read_events():
        return [json.loads(line) for line in stream.getvalue().splitlines()]

    yield read_events

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler in original_handlers:
        logger.addHandler(handler)
    logger.setLevel(original_level)
    logger.propagate = original_propagate


def _deepseek_ok_body() -> dict:
    return {"choices": [{"message": {"content": "ok"}}]}


def test_post_json_logs_request_and_response_with_controllable_clock(
    monkeypatch, captured_model_events
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_deepseek_ok_body())

    install_transport(monkeypatch, handler)
    ticks = iter([100.0, 100.25])

    status, body = asyncio.run(
        post_json(
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            FAKE_API_KEY,
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            request_id=REQUEST_ID,
            provider="deepseek",
            model="m",
            clock=lambda: next(ticks),
        )
    )

    assert status == 200
    assert body == _deepseek_ok_body()
    events = captured_model_events()
    assert [event["event"] for event in events] == [
        "llm_http_request",
        "llm_http_response",
    ]

    request_event = events[0]
    assert request_event["request_body"] == {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert request_event["method"] == "POST"
    assert request_event["url"] == DEEPSEEK_CHAT_COMPLETIONS_URL
    assert request_event["timeout"] == {
        "connect_seconds": 10.0,
        "total_seconds": 60.0,
    }
    assert request_event["headers"]["Authorization"] == "Bearer [REDACTED]"

    response_event = events[1]
    assert response_event["status_code"] == 200
    assert response_event["duration_ms"] == 250.0
    assert response_event["response_content_type"] == "application/json"


@pytest.mark.parametrize("status_code", [401, 403, 500])
def test_post_json_logs_response_without_error_event_for_non_2xx(
    monkeypatch, captured_model_events, status_code
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "secret upstream body"})

    install_transport(monkeypatch, handler)
    ticks = iter([0.0, 1.5])

    with pytest.raises(LlmProviderError):
        asyncio.run(
            post_json(
                DEEPSEEK_CHAT_COMPLETIONS_URL,
                FAKE_API_KEY,
                {"model": "m", "messages": []},
                request_id=REQUEST_ID,
                provider="deepseek",
                model="m",
                clock=lambda: next(ticks),
            )
        )

    events = captured_model_events()
    assert [event["event"] for event in events] == [
        "llm_http_request",
        "llm_http_response",
    ]
    assert events[1]["status_code"] == status_code
    assert events[1]["duration_ms"] == 1500.0


def test_post_json_logs_timeout_error_with_controllable_clock(
    monkeypatch, captured_model_events
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret timeout detail", request=request)

    install_transport(monkeypatch, handler)
    ticks = iter([10.0, 10.5])

    with pytest.raises(ProviderTimeoutError):
        asyncio.run(
            post_json(
                DEEPSEEK_CHAT_COMPLETIONS_URL,
                FAKE_API_KEY,
                {"model": "m", "messages": []},
                request_id=REQUEST_ID,
                provider="deepseek",
                model="m",
                clock=lambda: next(ticks),
            )
        )

    events = captured_model_events()
    assert [event["event"] for event in events] == [
        "llm_http_request",
        "llm_http_error",
    ]
    error_event = events[1]
    assert error_event["error_type"] == "timeout"
    assert error_event["duration_ms"] == 500.0
    assert "secret" not in json.dumps(error_event, ensure_ascii=False)


def test_post_json_logs_connection_error_with_controllable_clock(
    monkeypatch, captured_model_events
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret connection detail", request=request)

    install_transport(monkeypatch, handler)
    ticks = iter([20.0, 20.75])

    with pytest.raises(ProviderConnectionError):
        asyncio.run(
            post_json(
                DEEPSEEK_CHAT_COMPLETIONS_URL,
                FAKE_API_KEY,
                {"model": "m", "messages": []},
                request_id=REQUEST_ID,
                provider="deepseek",
                model="m",
                clock=lambda: next(ticks),
            )
        )

    events = captured_model_events()
    assert [event["event"] for event in events] == [
        "llm_http_request",
        "llm_http_error",
    ]
    error_event = events[1]
    assert error_event["error_type"] == "connection"
    assert error_event["duration_ms"] == 750.0
    assert "secret" not in json.dumps(error_event, ensure_ascii=False)


def test_post_json_log_events_redact_authorization_and_keep_payload(
    monkeypatch, captured_model_events
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {FAKE_API_KEY}"
        return httpx.Response(200, json=_deepseek_ok_body())

    install_transport(monkeypatch, handler)

    asyncio.run(
        post_json(
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            FAKE_API_KEY,
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            request_id=REQUEST_ID,
            provider="deepseek",
            model="m",
        )
    )

    log_text = json.dumps(captured_model_events(), ensure_ascii=False)
    assert FAKE_API_KEY not in log_text
    assert "Bearer [REDACTED]" in log_text


def test_deepseek_logged_request_body_equals_actual_payload_for_single_and_multi_turn(
    monkeypatch, captured_model_events
):
    captured_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        return httpx.Response(200, json=_deepseek_ok_body())

    install_transport(monkeypatch, handler)
    provider = DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-pro")

    asyncio.run(
        provider.generate(user_messages("第一问"), request_id=REQUEST_ID)
    )
    history = (
        ChatMessage(ChatRole.USER, "第一问"),
        ChatMessage(ChatRole.ASSISTANT, "第一答"),
        ChatMessage(ChatRole.USER, "第二问"),
    )
    asyncio.run(provider.generate(history, request_id=REQUEST_ID))

    request_events = [
        event
        for event in captured_model_events()
        if event["event"] == "llm_http_request"
    ]
    assert len(request_events) == 2
    assert request_events[0]["request_body"] == captured_payloads[0]
    assert request_events[1]["request_body"] == captured_payloads[1]
    assert request_events[1]["request_body"]["messages"] == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
    ]
    for event in request_events:
        assert event["provider"] == "deepseek"
        assert event["model"] == "deepseek-v4-pro"
        assert event["request_id"] == REQUEST_ID


def test_aliyun_logged_request_body_equals_actual_payload_for_single_and_multi_turn(
    monkeypatch, captured_model_events
):
    captured_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"output_text": "ok"})

    install_transport(monkeypatch, handler)
    provider = AliyunResponsesProvider(FAKE_API_KEY, "custom-qwen")

    asyncio.run(provider.generate(user_messages("hello"), request_id=REQUEST_ID))
    history = (
        ChatMessage(ChatRole.USER, "first"),
        ChatMessage(ChatRole.ASSISTANT, "answer"),
        ChatMessage(ChatRole.USER, "second"),
    )
    asyncio.run(provider.generate(history, request_id=REQUEST_ID))

    request_events = [
        event
        for event in captured_model_events()
        if event["event"] == "llm_http_request"
    ]
    assert len(request_events) == 2
    assert request_events[0]["request_body"] == captured_payloads[0]
    # 单轮 input 保留为字符串。
    assert request_events[0]["request_body"]["input"] == "hello"
    assert request_events[1]["request_body"] == captured_payloads[1]
    # 多轮 input 完整保留有序历史数组。
    assert request_events[1]["request_body"]["input"] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ]
    for event in request_events:
        assert event["provider"] == "aliyun"
        assert event["model"] == "custom-qwen"
        assert event["request_id"] == REQUEST_ID
