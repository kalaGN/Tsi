import httpx
import pytest
from fastapi.testclient import TestClient

import main
from app import application
from app.application import app as application_app
from app.services.llm import http_client
from app.services.llm.aliyun import ALIYUN_RESPONSES_URL
from app.services.llm.deepseek import DEEPSEEK_CHAT_COMPLETIONS_URL


client = TestClient(main.app)
FAKE_API_KEY = "test-only-api-key"


@pytest.fixture(autouse=True)
def clear_llm_environment(monkeypatch):
    for variable in (
        "LLM_PROVIDER",
        "DASHSCOPE_API_KEY",
        "ALIYUN_MODEL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
    ):
        monkeypatch.delenv(variable, raising=False)


def install_upstream_transport(monkeypatch, handler):
    """在 HTTP 契约测试中阻断所有真实模型请求。"""

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def create_client(**kwargs):
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(http_client.httpx, "AsyncClient", create_client)


def test_main_exports_application():
    assert main.app is application_app


def test_create_app_configures_model_logging(monkeypatch):
    calls = []
    monkeypatch.setattr(
        application,
        "configure_model_logging",
        lambda: calls.append("configured"),
    )

    created_app = application.create_app()

    assert created_app is not None
    assert calls == ["configured"]


def test_root_behavior_is_preserved():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"Hello": "World"}


def test_get_item_endpoint_is_removed():
    assert client.get("/items/1").status_code == 404


def test_put_item_endpoint_is_removed():
    response = client.put("/items/1", json={"name": "item", "price": 1.0})

    assert response.status_code == 404


def test_chat_supports_explicit_aliyun_and_returns_only_normalized_text(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "aliyun")
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == ALIYUN_RESPONSES_URL
        assert request.headers["Authorization"] == f"Bearer {FAKE_API_KEY}"
        assert request.content == b'{"model":"qwen3-max","input":"hello"}'
        return httpx.Response(
            200,
            json={
                "id": "response-1",
                "output": [{"text": "aliyun answer"}],
                "usage": {"total_tokens": 3},
            },
        )

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == 200
    assert response.json() == {"output_text": "aliyun answer"}


def test_chat_defaults_to_deepseek_and_returns_only_normalized_text(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == DEEPSEEK_CHAT_COMPLETIONS_URL
        assert request.headers["Authorization"] == f"Bearer {FAKE_API_KEY}"
        assert request.content == (
            b'{"model":"deepseek-v4-flash","messages":'
            b'[{"role":"user","content":"hello"}],"stream":false}'
        )
        return httpx.Response(
            200,
            json={
                "id": "chat-1",
                "choices": [
                    {
                        "message": {
                            "content": "deepseek answer",
                            "reasoning_content": "hidden reasoning",
                        }
                    }
                ],
                "usage": {"total_tokens": 5},
            },
        )

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == 200
    assert response.json() == {"output_text": "deepseek answer"}
    assert "choices" not in response.text
    assert "reasoning" not in response.text
    assert "usage" not in response.text


def test_chat_requests_remain_stateless(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_API_KEY)
    captured_messages = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_messages.append(request.read().decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "answer"}}]},
        )

    install_upstream_transport(monkeypatch, handler)

    assert client.post("/chat", json={"input": "first"}).status_code == 200
    assert client.post("/chat", json={"input": "second"}).status_code == 200

    assert '"messages":[{"role":"user","content":"first"}]' in (
        captured_messages[0]
    )
    assert '"messages":[{"role":"user","content":"second"}]' in (
        captured_messages[1]
    )
    assert "first" not in captured_messages[1]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"input": ""},
        {"input": "   "},
        {"input": {"unexpected": "object"}},
    ],
)
def test_chat_rejects_invalid_input(payload):
    response = client.post("/chat", json=payload)

    assert response.status_code == 422


def test_chat_requires_selected_provider_key_without_network(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Upstream must not be called without an API key")

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Upstream API key is not configured"}


def test_chat_rejects_invalid_provider_without_network(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "unknown")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Upstream must not be called for invalid configuration")

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Unsupported LLM provider configuration"}


def test_chat_maps_upstream_timeout_to_504(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == 504
    assert response.json() == {"detail": "Upstream request timed out"}


def test_chat_maps_upstream_connection_failure_to_502(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == 502
    assert response.json() == {"detail": "Unable to connect to upstream service"}


@pytest.mark.parametrize("status_code", [401, 403])
def test_chat_maps_upstream_authentication_errors(monkeypatch, status_code):
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "secret upstream body"})

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == status_code
    assert response.json() == {"detail": "Upstream authentication failed"}
    assert FAKE_API_KEY not in response.text
    assert "secret" not in response.text


@pytest.mark.parametrize("status_code", [302, 402, 422, 429, 500, 503])
def test_chat_maps_other_upstream_http_errors(monkeypatch, status_code):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "secret upstream body"})

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == status_code
    assert response.json() == {"detail": "Upstream service returned an error"}
    assert FAKE_API_KEY not in response.text
    assert "secret" not in response.text


def test_chat_rejects_non_json_upstream_success(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == 502
    assert response.json() == {"detail": "Upstream service returned invalid JSON"}
    assert FAKE_API_KEY not in response.text


def test_chat_rejects_success_without_text(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == 502
    assert response.json() == {
        "detail": "Upstream service returned an invalid response"
    }
