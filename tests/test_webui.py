import asyncio
import json
from pathlib import Path

import httpx
import pytest
from budget_support import BudgetedTestTurn, estimate_test_request
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.runtime.chat import ChatErrorCode, ChatRuntimeError, ChatRuntimeInfo
from app.runtime.model_selection import ModelSelectionService
from app.runtime.personalization_store import PersonalizationStore
from app.runtime.session import ChatExecutionSnapshot, ChatSession
from app.runtime.session_store import SessionStore
from app.runtime.system_prompt import compose_system_prompt, load_system_prompt
from app.services.llm.contracts import ChatRole
from app.services.llm.contracts import ModelStep, TokenUsage
from app.webui.router import create_webui_router
from app.webui.approvals import WebApprovalConflict, WebApprovalNotFound
from app.webui.projects import DEFAULT_PROJECT_ID, WebProjectCatalog, WebProjectRecord
from app.webui.service import WebUiBusyError, WebUiService, _runtime_budget_snapshot
from app.webui.sessions import WebSessionCatalog, WebSessionStoreError
from app.webui.statistics import WebStatisticsStore, WebStatisticsStoreError
from tools.contracts import ToolCall
from tools.workspace import (
    WorkspacePolicy,
    create_web_intent_workspace_registry,
)


class WebProvider:
    estimate_request = staticmethod(estimate_test_request)
    name = "deepseek"
    model = "test-model"
    api_key_configured = True

    def __init__(self, answer="你好，Web UI"):
        self.answer = answer

    def create_turn(self, messages, tools, *, request_id, **budget):
        return BudgetedTestTurn(WebTurn(self.answer), messages, tools, **budget)


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


class WorkspaceWriteProvider:
    estimate_request = staticmethod(estimate_test_request)
    name = "deepseek"
    model = "test-model"
    api_key_configured = True

    def create_turn(self, messages, tools, *, request_id, **budget):
        return BudgetedTestTurn(WorkspaceWriteTurn(), messages, tools, **budget)


class WorkspaceWriteTurn:
    def __init__(self):
        self.step = 0
        self.tools = ()

    def replace_tools(self, tools):
        self.tools = tuple(tools)

    async def next(self, tool_results=(), *, on_text_delta=None):
        self.step += 1
        if self.step == 1:
            return ModelStep(
                200,
                None,
                (
                    ToolCall(
                        "activate-1",
                        "activate_tool_groups",
                        '{"groups":["workspace_write"]}',
                    ),
                ),
                TokenUsage(4, 2, 6),
            )
        if self.step == 2:
            return ModelStep(
                200,
                None,
                (
                    ToolCall(
                        "edit-1",
                        "apply_workspace_edits",
                        json.dumps(
                            {
                                "edits": [
                                    {
                                        "mode": "create",
                                        "path": "generated.txt",
                                        "content": "由 Web 创建\n",
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ),
                TokenUsage(4, 2, 6),
            )
        return ModelStep(200, "修改流程结束", (), TokenUsage(4, 2, 6))


def create_service(tmp_path, provider=None):
    catalog = WebSessionCatalog(tmp_path / "web-sessions")
    projects = WebProjectCatalog(tmp_path / "web-projects.json", tmp_path)
    active_provider = provider or WebProvider()
    policy = WorkspacePolicy(tmp_path)

    def session_factory(store):
        def snapshot(_input):
            project = projects.require(catalog.current.project_id)
            current_policy = WorkspacePolicy(Path(project.path))
            return ChatExecutionSnapshot(
                system_prompt=load_system_prompt(current_policy.root),
                registry=create_web_intent_workspace_registry(current_policy),
            )

        return ChatSession.load(
            store,
            provider=active_provider,
            execution_snapshot_provider=snapshot,
        )

    return WebUiService(
        catalog,
        session_factory,
        ChatRuntimeInfo("deepseek", "test-model", True),
        (),
        ModelSelectionService(()),
        WebStatisticsStore(tmp_path / "web-statistics.json"),
        tmp_path,
        policy,
        context_window_tokens=1_000,
        projects=projects,
    )


def settings_client(tmp_path):
    service = create_service(tmp_path)
    application = FastAPI()
    application.include_router(create_webui_router(service))
    return TestClient(application), service


def test_project_api_groups_sessions_and_switches_workspace(tmp_path):
    client, service = settings_client(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "other.txt").write_text("第二项目", encoding="utf-8")
    first_session_id = service.catalog.current.id

    created = client.post("/ui/api/projects", json={"name": "第二项目", "path": str(other)})
    assert created.status_code == 200
    payload = created.json()
    project_id = payload["current_project_id"]
    assert len(payload["projects"]) == 2
    assert payload["current_session"]["project_id"] == project_id
    assert payload["workspace_path"] == str(other)
    assert any(item["path"] == "other.txt" for item in client.get("/ui/api/files").json()["items"])

    previous = client.post(f"/ui/api/sessions/{first_session_id}/select")
    assert previous.status_code == 200
    assert previous.json()["workspace_path"] == str(tmp_path)
    selected = client.post(f"/ui/api/projects/{project_id}/select", json={})
    assert selected.status_code == 200
    assert selected.json()["current_session"]["project_id"] == project_id


def test_project_path_edit_affects_next_request_and_keeps_history(tmp_path):
    class RecordingProvider(WebProvider):
        def __init__(self):
            super().__init__()
            self.prompts = []

        def create_turn(self, messages, tools, *, request_id, **budget):
            self.prompts.append([message.content for message in messages if message.role is ChatRole.SYSTEM])
            return super().create_turn(messages, tools, request_id=request_id, **budget)

    provider = RecordingProvider()
    service = create_service(tmp_path, provider)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "AGENTS.md").write_text("第一项目规则", encoding="utf-8")
    (second / "AGENTS.md").write_text("第二项目规则", encoding="utf-8")
    project_id = service.create_project(name="工作区", path=str(first))["current_project_id"]
    session_id = service.catalog.current.id

    async def send(text):
        return [event async for event in service.stream_message(text)]

    assert asyncio.run(send("第一次"))[-1]["type"] == "completed"
    updated = service.update_project(project_id, name="重命名", path=str(second))
    assert updated["current_session_id"] == session_id
    assert any(message["content"] == "第一次" for message in updated["messages"])
    assert asyncio.run(send("第二次"))[-1]["type"] == "completed"
    assert provider.prompts == [["第一项目规则"], ["第二项目规则"]]
    assert updated["workspace_name"] == "重命名"


def test_project_api_rejects_cross_origin_invalid_path_busy_and_missing_dir(tmp_path):
    client, service = settings_client(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    payload = {"name": "第二项目", "path": str(other)}
    assert client.post("/ui/api/projects", json=payload, headers={"Origin": "https://example.com"}).status_code == 403
    assert client.post("/ui/api/projects", json={**payload, "unexpected": True}).status_code == 422
    broad = client.post("/ui/api/projects", json={**payload, "path": "/"})
    assert broad.status_code == 422
    assert "根目录" in broad.json()["detail"]
    assert client.post("/ui/api/projects", content='{"name":"a","name":"b","path":"/"}', headers={"Content-Type": "application/json"}).status_code == 422
    asyncio.run(service._request_lock.acquire())
    try:
        assert client.post("/ui/api/projects", json=payload).status_code == 409
    finally:
        service._request_lock.release()
    created = client.post("/ui/api/projects", json=payload)
    project_id = created.json()["current_project_id"]
    other.rmdir()
    assert client.get("/ui/api/files").status_code == 422
    assert client.post(f"/ui/api/projects/{project_id}/select", json={}).status_code == 422
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("不能越界", encoding="utf-8")
    other.symlink_to(outside, target_is_directory=True)
    assert client.get("/ui/api/files").status_code == 422
    assert client.get("/ui/api/files/preview", params={"path": "private.txt"}).status_code == 404
    with pytest.raises(ValueError):
        service._validate_project(WebProjectRecord(DEFAULT_PROJECT_ID, "宽范围", "/"))


def test_project_menu_is_present_in_web_page(tmp_path):
    client, _ = settings_client(tmp_path)
    page = client.get("/ui").text
    script = client.get("/ui/app.js").text
    assert 'id="new-project"' in page
    assert 'id="project-dialog-path"' in page
    assert 'id="choose-project-path"' in page
    assert '/projects/pick-directory' in script
    assert 'project-group-heading' in script


def test_project_directory_picker_api_only_returns_path(tmp_path, monkeypatch):
    client, service = settings_client(tmp_path)
    before = service.bootstrap()["projects"]

    async def choose():
        return str(tmp_path)

    monkeypatch.setattr("app.webui.router.pick_project_directory", choose)
    assert client.post("/ui/api/projects/pick-directory", json={}, headers={"Origin": "https://example.com"}).status_code == 403
    assert client.post("/ui/api/projects/pick-directory", json={"path": "/"}).status_code == 422
    asyncio.run(service._request_lock.acquire())
    try:
        assert client.post("/ui/api/projects/pick-directory", json={}).status_code == 409
    finally:
        service._request_lock.release()
    selected = client.post("/ui/api/projects/pick-directory", json={})
    assert selected.status_code == 200
    assert selected.json() == {"cancelled": False, "path": str(tmp_path)}
    assert service.bootstrap()["projects"] == before

    async def cancel():
        return None

    monkeypatch.setattr("app.webui.router.pick_project_directory", cancel)
    assert client.post("/ui/api/projects/pick-directory", json={}).json() == {"cancelled": True, "path": None}
    assert service.bootstrap()["projects"] == before


def test_personalization_api_is_versioned_and_same_origin(tmp_path):
    client, service = settings_client(tmp_path)
    page = client.get("/ui").text
    script = client.get("/ui/app.js").text
    assert 'data-settings-route="personalization"' in page
    assert 'id="personalization-prompt"' in page
    assert 'personalization-form").addEventListener("submit", savePersonalization)' in script

    initial = client.get("/ui/api/personalization").json()
    assert initial == {"version": 1, "revision": 0, "prompt": ""}
    payload = {"expected_revision": 0, "prompt": "始终用中文回答。"}
    assert client.put("/ui/api/personalization", json=payload, headers={"Origin": "https://example.com"}).status_code == 403
    saved = client.put("/ui/api/personalization", json=payload)
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    assert service.personalization_store.current_prompt == payload["prompt"]
    assert client.put("/ui/api/personalization", json=payload).status_code == 409
    assert client.put("/ui/api/personalization", json={**payload, "extra": True}).status_code == 422
    assert client.put("/ui/api/personalization", json={"expected_revision": 1, "prompt": "中" * 6000}).status_code == 422
    assert PersonalizationStore(tmp_path / "data" / "personalization.json").load()["prompt"] == payload["prompt"]


def test_personalization_save_only_changes_following_snapshots(tmp_path):
    client, service = settings_client(tmp_path)
    (tmp_path / "AGENTS.md").write_text("项目规则", encoding="utf-8")

    # 测试装配与生产代码一样，在每轮请求开始时读取项目规则和当前个性化快照。
    service.session._execution_snapshot_provider = lambda _input: ChatExecutionSnapshot(
        system_prompt=compose_system_prompt(
            load_system_prompt(tmp_path), service.personalization_store.current_prompt,
        ),
        registry=None,
    )
    first = service.session._execution_snapshot_provider("")
    assert first.system_prompt == "项目规则"
    client.put("/ui/api/personalization", json={"expected_revision": 0, "prompt": "个人偏好"})
    second = service.session._execution_snapshot_provider("")
    assert first.system_prompt == "项目规则"
    assert second.system_prompt == "项目规则\n\n---\n\n个人偏好"
    client.put("/ui/api/personalization", json={"expected_revision": 1, "prompt": ""})
    assert service.session._execution_snapshot_provider("").system_prompt == "项目规则"


def test_context_settings_api_saves_and_resets_scope_with_revision(tmp_path):
    client, service = settings_client(tmp_path)
    initial = client.get("/ui/api/context-settings").json()
    assert initial["revision"] == 0
    assert initial["input_limit"] == 119808
    payload = {"expected_revision": 0, "scope": "model", "provider": "deepseek", "model": "test-model", "overrides": {"context_window_tokens": 32000}}
    saved = client.put("/ui/api/context-settings", json=payload, headers={"Origin": "http://testserver"})
    assert saved.status_code == 200
    assert saved.json()["input_limit"] == 23808
    assert saved.json()["sources"]["context_window_tokens"] == "settings"
    assert client.put("/ui/api/context-settings", json=payload).status_code == 409
    payload.update(expected_revision=1, overrides={})
    assert client.put("/ui/api/context-settings", json=payload).json()["effective"]["context_window_tokens"] == 128000
    assert service.context_settings.store.path.exists()


def test_saved_page_model_budget_is_used_on_next_web_send(tmp_path):
    class RecordingBudgetProvider(WebProvider):
        def __init__(self):
            super().__init__()
            self.options = []

        def create_turn(self, messages, tools, *, request_id, **budget):
            self.options.append(budget["options"].max_output_tokens)
            return super().create_turn(messages, tools, request_id=request_id, **budget)

    provider = RecordingBudgetProvider()
    service = create_service(tmp_path, provider)
    service.context_settings.save(
        expected_revision=0, scope="model",
        overrides={"context_window_tokens": 16_000, "max_output_tokens": 1000},
        provider="deepseek", model="test-model", configured_models=(),
    )
    service._session_factory = lambda store: ChatSession.load(
        store, provider=provider,
        budget_snapshot_provider=lambda active: _runtime_budget_snapshot(service.context_settings, active),
    )

    async def collect():
        return [event async for event in service.stream_message("你好")]

    events = asyncio.run(collect())

    assert provider.options == [1000]
    assert events[-1]["type"] == "completed"
    assert events[-1]["context_percent"] == service.session.context_snapshot["percent"]
    assert service.session.context_snapshot["input_limit"] == 10_904


@pytest.mark.parametrize("overrides", [{"max_output_tokens": True}, {"max_output_tokens": 0}, {"context_window_tokens": 4096}, {"api_key": "not-a-secret"}, {"path": "not-allowed"}])
def test_context_settings_api_rejects_invalid_or_secret_fields(tmp_path, overrides):
    client, service = settings_client(tmp_path)
    response = client.put("/ui/api/context-settings", json={"scope": "model", "expected_revision": 0, "provider": "deepseek", "model": "test-model", "overrides": overrides})
    assert response.status_code == 422
    assert not service.context_settings.store.path.exists()


@pytest.mark.parametrize("headers", [{"Origin": "https://example.com"}, {"Origin": "null"}, {"Origin": "http://testserver:9000"}, {"Sec-Fetch-Site": "cross-site"}])
def test_context_settings_api_rejects_cross_origin_writes(tmp_path, headers):
    client, service = settings_client(tmp_path)
    response = client.put("/ui/api/context-settings", headers=headers, json={"scope": "compaction", "expected_revision": 0, "overrides": {}})
    assert response.status_code == 403
    assert not service.context_settings.store.path.exists()


def test_context_settings_api_rejects_duplicate_json_busy_and_stale_model(tmp_path):
    client, service = settings_client(tmp_path)
    response = client.put("/ui/api/context-settings", content='{"scope":"compaction","scope":"model"}', headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert client.put("/ui/api/context-settings", content="{}", headers={"Content-Type": "text/plain"}).status_code == 422
    payload = {"scope": "model", "expected_revision": 0, "provider": "deepseek", "model": "old-model", "overrides": {}}
    assert client.put("/ui/api/context-settings", json=payload).status_code == 409
    asyncio.run(service._request_lock.acquire())
    try:
        payload.update(model="test-model")
        assert client.put("/ui/api/context-settings", json=payload).status_code == 409
    finally:
        service._request_lock.release()


def test_context_settings_api_does_not_replace_corrupt_file(tmp_path):
    client, service = settings_client(tmp_path)
    assert client.get("/ui/api/context-settings").status_code == 200
    path = service.context_settings.store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("broken", encoding="utf-8")
    assert client.get("/ui/api/context-settings").json()["warning"]
    assert client.put("/ui/api/context-settings", json={"scope": "compaction", "expected_revision": 0, "overrides": {}}).status_code == 503
    assert path.read_text() == "broken"


def test_context_settings_menu_and_separate_script_are_available(tmp_path):
    client, _ = settings_client(tmp_path)
    page = client.get("/ui").text
    assert page.index('data-settings-route="model"') < page.index('data-settings-route="context"') < page.index('data-settings-route="statistics"')
    assert 'data-budget-form="model"' in page
    assert 'data-budget-form="compaction"' in page
    script = client.get("/ui/context-settings.js")
    assert script.status_code == 200
    assert 'method: "PUT"' in script.text


def test_webui_bootstrap_is_provider_neutral_and_does_not_expose_secrets(tmp_path):
    service = create_service(tmp_path)

    payload = service.bootstrap()

    assert payload["project_name"] == "Tsi 助手"
    assert payload["runtime"] == {
        "provider": "deepseek",
        "model": "test-model",
        "api_key_configured": True,
    }
    assert payload["capabilities"]["workspace_write"] is True
    assert payload["capabilities"]["web_search"] is True
    assert len(payload["sessions"]) == 1
    assert payload["current_session_id"] == payload["sessions"][0]["id"]
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
            "context_updated",
            "text_delta",
            "text_delta",
            "context_updated",
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
        assert service.statistics_payload()["totals"]["completed"] == 1
        assert service.statistics_payload()["totals"]["total_tokens"] == 12

    asyncio.run(scenario())


def test_webui_sessions_keep_histories_isolated_and_restore_selection(tmp_path):
    async def scenario():
        service = create_service(tmp_path)
        first_id = service.catalog.current.id
        await _consume(service.stream_message("第一段对话"))

        created = service.create_session()
        second_id = created["current_session_id"]
        await _consume(service.stream_message("第二段对话"))
        first = service.select_session(first_id)

        assert second_id != first_id
        assert [item["content"] for item in first["messages"]] == [
            "第一段对话",
            "你好，Web UI",
        ]
        assert first["current_session"]["title"] == "第一段对话"
        assert service.select_session(second_id)["messages"][0]["content"] == "第二段对话"

    asyncio.run(scenario())


def test_webui_can_rename_clear_and_delete_sessions(tmp_path):
    service = create_service(tmp_path)
    first_id = service.catalog.current.id
    second_id = service.create_session()["current_session_id"]

    renamed = service.rename_session(second_id, "  新标题  ")
    cleared = service.clear()
    deleted = service.delete_session(second_id)

    assert renamed["current_session"]["title"] == "新标题"
    assert cleared["messages"] == []
    assert deleted["current_session_id"] == first_id
    assert all(item["id"] != second_id for item in deleted["sessions"])


def test_webui_rejects_session_changes_while_request_lock_is_held(tmp_path):
    async def scenario():
        service = create_service(tmp_path)
        await service._request_lock.acquire()
        try:
            with pytest.raises(WebUiBusyError):
                service.create_session()
            with pytest.raises(WebUiBusyError):
                service.select_session(service.catalog.current.id)
            with pytest.raises(WebUiBusyError):
                service.delete_session(service.catalog.current.id)
        finally:
            service._request_lock.release()

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


def test_webui_registry_can_activate_workspace_write_without_git(tmp_path):
    async def scenario():
        registry = create_web_intent_workspace_registry(
            WorkspacePolicy(tmp_path)
        )

        assert [item.name for item in registry.definitions] == [
            "activate_tool_groups"
        ]
        result = await registry.execute(
            ToolCall("call-1", "activate_tool_groups", '{"groups":["workspace_write"]}')
        )

        assert result.is_error is False
        names = {item.name for item in registry.definitions}
        assert "read_workspace_file" in names
        assert "read_workspace_files" in names
        assert "apply_workspace_edits" in names
        assert "delete_workspace_file" in names
        assert "git_commit" not in names

    asyncio.run(scenario())


@pytest.mark.parametrize("approved, file_exists", ((True, True), (False, False)))
def test_webui_workspace_write_waits_for_explicit_approval(
    tmp_path,
    approved,
    file_exists,
):
    async def scenario():
        service = create_service(tmp_path, WorkspaceWriteProvider())
        stream = service.stream_message("创建 generated.txt")
        events = []

        async for event in stream:
            events.append(event)
            if event["type"] == "tool_approval_required":
                assert not (tmp_path / "generated.txt").exists()
                assert event["tool"] == "apply_workspace_edits"
                assert event["paths"] == ["generated.txt"]
                assert "由 Web 创建" in event["diff"]
                service.resolve_tool_approval(
                    event["approval_id"],
                    event["request_id"],
                    approved,
                )

        assert (tmp_path / "generated.txt").exists() is file_exists
        assert events[-1]["type"] == "completed"
        assert "fingerprint" not in str(events)

    asyncio.run(scenario())


def test_webui_cancel_invalidates_pending_workspace_approval(tmp_path):
    async def scenario():
        service = create_service(tmp_path, WorkspaceWriteProvider())
        stream = service.stream_message("创建 generated.txt")
        approval = None
        events = []

        async for event in stream:
            events.append(event)
            if event["type"] == "tool_approval_required":
                approval = event
                assert service.cancel_current() is True

        assert approval is not None
        assert events[-1]["type"] == "cancelled"
        assert service.statistics_payload()["totals"]["cancelled"] == 1
        assert not (tmp_path / "generated.txt").exists()
        with pytest.raises(WebApprovalNotFound):
            service.resolve_tool_approval(
                approval["approval_id"],
                approval["request_id"],
                True,
            )

    asyncio.run(scenario())


def test_webui_router_serves_local_page_bootstrap_and_ndjson(tmp_path):
    application = FastAPI()
    application.include_router(create_webui_router(create_service(tmp_path)))
    client = TestClient(application)

    page = client.get("/ui")
    brand_mark = client.get("/ui/tsi-mark.svg")
    bootstrap = client.get("/ui/api/bootstrap")
    response = client.post("/ui/api/chat", json={"input": "你好"})

    assert page.status_code == 200
    assert "Tsi 助手" in page.text
    assert brand_mark.status_code == 200
    assert brand_mark.headers["content-type"].startswith("image/svg+xml")
    assert '<link rel="icon" type="image/svg+xml" href="/ui/tsi-mark.svg"' in page.text
    assert bootstrap.status_code == 200
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert '"type": "completed"' in response.text


def test_webui_router_exposes_aggregate_statistics(tmp_path):
    service = create_service(tmp_path)
    application = FastAPI()
    application.include_router(create_webui_router(service))
    client = TestClient(application)

    client.post("/ui/api/chat", json={"input": "你好"})
    response = client.get("/ui/api/statistics")

    assert response.status_code == 200
    assert response.json()["totals"]["requests"] == 1
    assert response.json()["models"][0]["model"] == "test-model"


def test_webui_router_exposes_session_lifecycle(tmp_path):
    application = FastAPI()
    application.include_router(create_webui_router(create_service(tmp_path)))
    client = TestClient(application)

    created = client.post("/ui/api/sessions").json()
    session_id = created["current_session_id"]
    renamed = client.patch(
        f"/ui/api/sessions/{session_id}",
        json={"title": "会话名称"},
    )
    selected = client.post(f"/ui/api/sessions/{session_id}/select")
    deleted = client.delete(f"/ui/api/sessions/{session_id}")
    missing = client.post(f"/ui/api/sessions/{session_id}/select")

    assert renamed.status_code == 200
    assert renamed.json()["current_session"]["title"] == "会话名称"
    assert selected.status_code == 200
    assert deleted.status_code == 200
    assert missing.status_code == 404


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


def test_webui_settings_and_workspace_approval_contract_are_present(tmp_path):
    application = FastAPI()
    application.include_router(create_webui_router(create_service(tmp_path)))
    client = TestClient(application)

    html = client.get("/ui").text
    javascript = client.get("/ui/app.js").text

    assert 'id="settings-dialog"' not in html
    assert 'id="settings-view"' in html
    assert 'data-settings-route="general"' in html
    assert 'data-settings-route="model"' in html
    assert 'data-settings-route="statistics"' in html
    assert 'id="statistics-chart"' in html
    assert 'data-stat-range="7"' in html
    assert 'data-stat-range="30"' in html
    assert 'data-stat-metric="requests"' in html
    assert 'data-stat-metric="total_tokens"' in html
    assert 'data-stat-metric="average_elapsed_ms"' in html
    assert 'id="theme-setting"' in html
    assert 'id="density-setting"' in html
    assert 'id="send-key-setting"' in html
    assert 'id="conversation-list"' in html
    assert 'id="tool-approval-dialog"' in html
    assert 'id="session-dialog"' in html
    assert 'id="approval-diff"' in html
    assert 'id="session-title"' in html
    assert 'aria-label="新建会话"' in html
    assert 'aria-label="打开设置"' in html
    assert 'class="welcome-mark" src="/ui/tsi-mark.svg"' in html
    assert 'class="send-button" id="send" aria-label="发送"' in html
    assert 'data-tab="files" aria-label="文件"' in html
    assert "tsi-web-preferences" in javascript
    assert "createIcon" in javascript
    assert "fileIconName" in javascript
    assert 'matchMedia("(max-width: 1040px)")' in javascript
    assert 'api("/sessions"' in javascript
    assert 'method: "DELETE"' in javascript
    assert "tool_approval_required" in javascript
    assert "window.prompt" not in javascript
    assert "window.confirm" not in javascript
    assert ".showModal()" not in javascript
    assert "/tool-approvals/" in javascript
    assert '$("#activity-text").textContent = "正在回答"' in javascript
    assert 'return ["completed", "failed", "cancelled"].includes(event.type)' in javascript
    assert "await reader.cancel()" in javascript
    assert '#/settings/general' in javascript
    assert 'api("/statistics")' in javascript
    assert "createElementNS(SVG_NAMESPACE" in javascript
    assert client.post("/ui/api/settings", json={}).status_code == 404


def test_webui_tool_approval_route_maps_success_and_safe_failures(
    tmp_path,
    monkeypatch,
):
    service = create_service(tmp_path)
    application = FastAPI()
    application.include_router(create_webui_router(service))
    client = TestClient(application)

    monkeypatch.setattr(
        service,
        "resolve_tool_approval",
        lambda approval_id, request_id, approved: {"accepted": approved},
    )
    accepted = client.post(
        "/ui/api/tool-approvals/approval-1",
        json={"request_id": "request-1", "approved": True},
    )
    invalid = client.post(
        "/ui/api/tool-approvals/approval-1",
        json={"request_id": "request-1", "approved": "true"},
    )
    blank = client.post(
        "/ui/api/tool-approvals/approval-1",
        json={"request_id": " ", "approved": True},
    )

    def missing(*args):
        raise WebApprovalNotFound()

    monkeypatch.setattr(service, "resolve_tool_approval", missing)
    expired = client.post(
        "/ui/api/tool-approvals/approval-1",
        json={"request_id": "request-1", "approved": False},
    )

    def conflict(*args):
        raise WebApprovalConflict()

    monkeypatch.setattr(service, "resolve_tool_approval", conflict)
    mismatched = client.post(
        "/ui/api/tool-approvals/approval-1",
        json={"request_id": "request-2", "approved": False},
    )

    assert accepted.status_code == 200
    assert accepted.json() == {"accepted": True}
    assert invalid.status_code == 422
    assert blank.status_code == 422
    assert expired.status_code == 404
    assert mismatched.status_code == 409


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
            statistics = await client.get("/ui/api/statistics")
            approval = await client.post(
                "/ui/api/tool-approvals/approval-1",
                json={"request_id": "request-1", "approved": True},
            )

        assert page.status_code == 403
        assert bootstrap.status_code == 403
        assert statistics.status_code == 403
        assert approval.status_code == 403

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
        assert service.statistics_payload()["totals"]["failed"] == 1

    asyncio.run(scenario())


def test_webui_known_runtime_failure_is_counted_once(tmp_path, monkeypatch):
    async def scenario():
        service = create_service(tmp_path)

        async def fail(*args, **kwargs):
            raise ChatRuntimeError(ChatErrorCode.TIMEOUT, "上游请求超时。")

        monkeypatch.setattr(service.session, "send", fail)
        events = [event async for event in service.stream_message("你好")]

        assert events[-1]["type"] == "failed"
        assert events[-1]["code"] == "timeout"
        assert service.statistics_payload()["totals"]["requests"] == 1
        assert service.statistics_payload()["totals"]["failed"] == 1

    asyncio.run(scenario())


def test_webui_statistics_failure_does_not_change_terminal_event(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        service = create_service(tmp_path)

        def fail_record(*args, **kwargs):
            raise WebStatisticsStoreError("private statistics path")

        monkeypatch.setattr(service.statistics, "record", fail_record)
        events = [event async for event in service.stream_message("你好")]

        assert events[-1]["type"] == "completed"
        assert events[-1]["output_text"] == "你好，Web UI"
        assert "private statistics" not in str(events)

    asyncio.run(scenario())


def test_webui_metadata_failure_still_completes_stream(tmp_path, monkeypatch):
    async def scenario():
        service = create_service(tmp_path)

        def fail_touch(*args, **kwargs):
            raise WebSessionStoreError("private failure details")

        monkeypatch.setattr(service.catalog, "touch", fail_touch)
        events = [event async for event in service.stream_message("你好")]

        assert events[-1]["type"] == "completed"
        assert events[-1]["warning"] == "消息已保存，但会话列表更新失败。"
        assert [message.content for message in service.session.messages] == [
            "你好",
            "你好，Web UI",
        ]
        assert "private failure" not in str(events)

    asyncio.run(scenario())


async def _consume(stream):
    return [event async for event in stream]
