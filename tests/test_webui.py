import asyncio

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.runtime.chat import ChatRuntimeInfo
from app.runtime.model_selection import ModelSelectionService
from app.runtime.session import ChatSession
from app.runtime.session_store import SessionStore
from app.services.llm.contracts import ModelStep, TokenUsage
from app.webui.router import create_webui_router
from app.webui.service import WebUiService
from tools.contracts import ToolCall
from tools.workspace import (
    WorkspacePolicy,
    create_readonly_intent_workspace_registry,
)


class WebProvider:
    name = "deepseek"
    model = "test-model"
    api_key_configured = True

    def __init__(self, answer="你好，Web UI"):
        self.answer = answer

    def create_turn(self, messages, tools, *, request_id):
        return WebTurn(self.answer)


class WebTurn:
    def __init__(self, answer):
        self.answer = answer

    async def next(self, tool_results=(), *, on_text_delta=None):
        if on_text_delta is not None:
            on_text_delta(self.answer[:2])
            on_text_delta(self.answer[2:])
        return ModelStep(
            200,
            self.answer,
            (),
            TokenUsage(8, 4, 12),
        )


def create_service(tmp_path):
    session = ChatSession(
        SessionStore(tmp_path / "web-session.json"),
        provider=WebProvider(),
    )
    return WebUiService(
        session,
        ChatRuntimeInfo("deepseek", "test-model", True),
        (),
        ModelSelectionService(()),
        tmp_path,
        WorkspacePolicy(tmp_path),
        context_window_tokens=1_000,
    )


def test_webui_bootstrap_is_provider_neutral_and_does_not_expose_secrets(tmp_path):
    service = create_service(tmp_path)

    payload = service.bootstrap()

    assert payload["project_name"] == "Tsi 助手"
    assert payload["runtime"] == {
        "provider": "deepseek",
        "model": "test-model",
        "api_key_configured": True,
    }
    assert payload["capabilities"]["workspace_write"] is False
    assert "test-only-secret" not in str(payload)
    assert set(payload["runtime"]) == {
        "provider",
        "model",
        "api_key_configured",
    }


def test_webui_streams_deltas_and_commits_only_final_message(tmp_path):
    async def scenario():
        service = create_service(tmp_path)

        events = [event async for event in service.stream_message("你好")]

        assert [event["type"] for event in events] == [
            "request_started",
            "text_delta",
            "text_delta",
            "completed",
        ]
        assert events[-1]["output_text"] == "你好，Web UI"
        assert events[-1]["token_usage"] == {
            "input": 8,
            "output": 4,
            "total": 12,
        }
        assert [message.content for message in service.session.messages] == [
            "你好",
            "你好，Web UI",
        ]

    asyncio.run(scenario())


def test_webui_file_browser_reuses_workspace_protection(tmp_path):
    async def scenario():
        (tmp_path / "README.md").write_text("项目说明\n", encoding="utf-8")
        (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
        service = create_service(tmp_path)

        listing = await service.list_files()
        paths = {item["path"] for item in listing["items"]}

        assert "README.md" in paths
        assert ".env" not in paths
        preview = await service.preview_file("README.md")
        assert preview["content"] == "项目说明\n"

    asyncio.run(scenario())


def test_webui_registry_can_only_activate_readonly_tools(tmp_path):
    async def scenario():
        registry = create_readonly_intent_workspace_registry(
            WorkspacePolicy(tmp_path)
        )

        assert [item.name for item in registry.definitions] == [
            "activate_tool_groups"
        ]
        result = await registry.execute(
            ToolCall("call-1", "activate_tool_groups", '{"groups":["workspace_read"]}')
        )

        assert result.is_error is False
        names = {item.name for item in registry.definitions}
        assert "read_workspace_file" in names
        assert "apply_workspace_edits" not in names
        assert "delete_workspace_file" not in names
        assert "git_commit" not in names

    asyncio.run(scenario())


def test_webui_router_serves_local_page_bootstrap_and_ndjson(tmp_path):
    application = FastAPI()
    application.include_router(create_webui_router(create_service(tmp_path)))
    client = TestClient(application)

    page = client.get("/ui")
    bootstrap = client.get("/ui/api/bootstrap")
    response = client.post("/ui/api/chat", json={"input": "你好"})

    assert page.status_code == 200
    assert "Tsi 助手" in page.text
    assert bootstrap.status_code == 200
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert '"type": "completed"' in response.text


def test_webui_static_assets_do_not_load_remote_scripts_or_styles(tmp_path):
    application = FastAPI()
    application.include_router(create_webui_router(create_service(tmp_path)))
    client = TestClient(application)

    html = client.get("/ui").text
    javascript = client.get("/ui/app.js").text

    assert 'src="http' not in html
    assert 'href="http' not in html
    assert "innerHTML" not in javascript
    assert "eval(" not in javascript


def test_webui_settings_are_local_and_do_not_add_a_write_api(tmp_path):
    application = FastAPI()
    application.include_router(create_webui_router(create_service(tmp_path)))
    client = TestClient(application)

    html = client.get("/ui").text
    javascript = client.get("/ui/app.js").text

    assert 'id="settings-dialog"' in html
    assert 'id="theme-setting"' in html
    assert 'id="density-setting"' in html
    assert 'id="send-key-setting"' in html
    assert "tsi-web-preferences" in javascript
    assert '/settings' not in javascript
    assert client.post("/ui/api/settings", json={}).status_code == 404


def test_webui_rejects_non_loopback_clients(tmp_path):
    async def scenario():
        application = FastAPI()
        application.include_router(create_webui_router(create_service(tmp_path)))
        transport = httpx.ASGITransport(app=application, client=("203.0.113.8", 5000))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://local.test",
        ) as client:
            page = await client.get("/ui")
            bootstrap = await client.get("/ui/api/bootstrap")

        assert page.status_code == 403
        assert bootstrap.status_code == 403

    asyncio.run(scenario())


def test_webui_unknown_runtime_failure_ends_stream_safely(tmp_path, monkeypatch):
    async def scenario():
        service = create_service(tmp_path)

        async def fail(*args, **kwargs):
            raise RuntimeError("private failure details")

        monkeypatch.setattr(service.session, "send", fail)
        events = [event async for event in service.stream_message("你好")]

        assert events[-1]["type"] == "failed"
        assert events[-1]["code"] == "internal"
        assert events[-1]["message"] == "Web UI 请求失败。"
        assert "private failure" not in str(events)

    asyncio.run(scenario())
