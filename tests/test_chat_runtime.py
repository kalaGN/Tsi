import asyncio

import pytest

from app.runtime import chat
from app.runtime.chat import (
    ChatErrorCode,
    ChatRuntimeError,
    get_chat_runtime_info,
    run_chat,
)
from app.services.llm.contracts import (
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderInvalidResponseError,
    ProviderResponseError,
    ProviderResult,
    ProviderTimeoutError,
)


class FakeProvider:
    name = "fake"
    model = "fake-model"
    api_key_configured = True

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.received_inputs = []

    async def generate(self, input_text: str) -> ProviderResult:
        self.received_inputs.append(input_text)
        if self.error is not None:
            raise self.error
        return self.result


def test_run_chat_returns_normalized_provider_result():
    provider = FakeProvider(
        result=ProviderResult(
            upstream_status=201,
            raw_body={"id": "response-1", "secret-shape": True},
            output_text="hello",
        )
    )

    result = asyncio.run(run_chat("hello", provider=provider))

    assert provider.received_inputs == ["hello"]
    assert result.output_text == "hello"
    assert result.provider == "fake"
    assert result.model == "fake-model"
    assert not hasattr(result, "raw_body")
    assert not hasattr(result, "upstream_status")


def test_run_chat_logs_input_and_output_around_provider_call(monkeypatch):
    call_order = []
    captured_requests = []
    captured_responses = []

    class OrderedProvider(FakeProvider):
        async def generate(self, input_text: str) -> ProviderResult:
            call_order.append("provider")
            return await super().generate(input_text)

    provider = OrderedProvider(
        result=ProviderResult(
            200,
            {"provider-raw-marker": "must-not-be-logged"},
            "secret-output",
        )
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
    assert "provider-raw-marker" not in repr(captured_responses)


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


def test_run_chat_rejects_blank_input_without_calling_provider():
    provider = FakeProvider(
        result=ProviderResult(200, {}, "should not be returned")
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
