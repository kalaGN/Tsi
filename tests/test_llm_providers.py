import asyncio
import json

import httpx
import pytest

from app.services.llm.aliyun import (
    ALIYUN_RESPONSES_URL,
    AliyunResponsesProvider,
)
from app.services.llm.contracts import (
    ChatMessage,
    ChatRole,
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


FAKE_API_KEY = "test-only-provider-key"


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

    result = asyncio.run(provider.generate(user_messages()))

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

    result = asyncio.run(provider.generate(user_messages("你好")))

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

    assert asyncio.run(provider.generate(history)).output_text == "ok"


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
        asyncio.run(provider.generate(messages))

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
        asyncio.run(provider.generate(user_messages()))

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
        asyncio.run(provider.generate(user_messages()))

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
        asyncio.run(provider.generate(user_messages()))

    assert captured.value.user_message == "Unable to connect to upstream service"
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize("status_code", [401, 403])
def test_provider_maps_authentication_error(monkeypatch, status_code):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "secret upstream body"})

    install_transport(monkeypatch, handler)
    provider = DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash")

    with pytest.raises(ProviderAuthenticationError) as captured:
        asyncio.run(provider.generate(user_messages()))

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
        asyncio.run(provider.generate(user_messages()))

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
        asyncio.run(provider.generate(user_messages()))

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
        asyncio.run(provider.generate(user_messages()))

    assert captured.value.user_message == "Upstream service returned an invalid response"
