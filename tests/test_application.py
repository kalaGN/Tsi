import inspect

from fastapi.testclient import TestClient

import main
from app import application
from app.application import app as application_app


client = TestClient(main.app)


def test_http_entrypoints_do_not_import_workspace_capabilities():
    sources = (
        inspect.getsource(main),
        inspect.getsource(application),
    )

    assert all("tools.workspace" not in source for source in sources)
    assert all("create_workspace_registry" not in source for source in sources)


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


def test_fastapi_and_openapi_use_official_project_name():
    response = client.get("/openapi.json")

    assert application_app.title == "Tsi 助手"
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Tsi 助手"


def test_chat_endpoint_is_removed():
    response = client.post("/chat", json={"input": "你好"})

    assert response.status_code == 404
    assert "/chat" not in client.get("/openapi.json").json()["paths"]


def test_get_item_endpoint_is_removed():
    assert client.get("/items/1").status_code == 404


def test_put_item_endpoint_is_removed():
    response = client.put("/items/1", json={"name": "item", "price": 1.0})

    assert response.status_code == 404
