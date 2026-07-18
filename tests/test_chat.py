import httpx
import pytest
from fastapi.testclient import TestClient

import main
from app.application import app as application_app
from app.services import aliyun_responses


client = TestClient(main.app)
FAKE_API_KEY = "test-only-api-key"


def test_main_exports_application():
    assert main.app is application_app


def test_root_behavior_is_preserved():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"Hello": "World"}


def test_get_item_endpoint_is_removed():
    response = client.get("/items/1")

    assert response.status_code == 404


def test_put_item_endpoint_is_removed():
    response = client.put("/items/1", json={"name": "item", "price": 1.0})

    assert response.status_code == 404


def install_upstream_transport(monkeypatch, handler):
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def create_client(**kwargs):
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(aliyun_responses.httpx, "AsyncClient", create_client)


def test_chat_forwards_request_and_returns_upstream_json(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == aliyun_responses.UPSTREAM_RESPONSES_URL
        assert request.headers["Authorization"] == f"Bearer {FAKE_API_KEY}"
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["Accept"] == "application/json"
        assert request.headers.get("Host") == request.url.host
        assert request.headers.get("User-Agent") != "Apifox/1.0.0 (https://apifox.com)"
        assert request.content == b'{"model":"qwen3-max","input":"\xe4\xbd\xa0\xe6\x98\xaf\xe8\xb0\x81\xef\xbc\x9f"}'
        return httpx.Response(
            200,
            json={"id": "response-1", "output": [{"text": "我是通义千问。"}]},
        )

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "你是谁？"})

    assert response.status_code == 200
    assert response.json() == {
        "id": "response-1",
        "output": [{"text": "我是通义千问。"}],
    }


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


def test_chat_requires_api_key_and_does_not_call_upstream(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Upstream must not be called without an API key")

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Upstream API key is not configured"}


def test_chat_maps_upstream_timeout_to_504(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == 504
    assert response.json() == {"detail": "Upstream request timed out"}


def test_chat_maps_upstream_connection_failure_to_502(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == 502
    assert response.json() == {"detail": "Unable to connect to upstream service"}


@pytest.mark.parametrize("status_code", [401, 403])
def test_chat_maps_upstream_authentication_errors(monkeypatch, status_code):
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "do not expose upstream body"})

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == status_code
    assert response.json() == {"detail": "Upstream authentication failed"}
    assert FAKE_API_KEY not in response.text


@pytest.mark.parametrize("status_code", [302, 429])
def test_chat_maps_other_upstream_http_errors(monkeypatch, status_code):
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "internal upstream detail"})

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == status_code
    assert response.json() == {"detail": "Upstream service returned an error"}
    assert FAKE_API_KEY not in response.text


def test_chat_rejects_non_json_upstream_success(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    install_upstream_transport(monkeypatch, handler)

    response = client.post("/chat", json={"input": "hello"})

    assert response.status_code == 502
    assert response.json() == {"detail": "Upstream service returned invalid JSON"}
    assert FAKE_API_KEY not in response.text
