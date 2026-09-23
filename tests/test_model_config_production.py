"""设置保存后的真实 Web/TUI 装配路径，不调用付费模型。"""

import asyncio

from budget_support import BudgetedTestTurn, estimate_test_request
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.webui.service as web_service_module
from app.runtime.context_settings_store import ContextSettingsStore
from app.runtime.model_config_store import ModelConfigStore
from app.runtime.model_selection_store import ModelSelectionStore
from app.runtime.personalization_store import PersonalizationStore
from app.services.llm.contracts import ModelStep, TokenUsage
from app.tui.bootstrap import build_tui_dependencies
from app.webui.router import create_webui_router
from app.webui.service import WebUiService


class RecordingProvider:
    estimate_request = staticmethod(estimate_test_request)
    name = "deepseek"

    def __init__(self, key, model, used_keys):
        self.key = key
        self.model = model
        self.used_keys = used_keys

    @property
    def api_key_configured(self):
        return bool(self.key)

    def create_turn(self, messages, tools, *, request_id, **kwargs):
        self.used_keys.append(self.key)
        return BudgetedTestTurn(RecordingTurn(), messages, tools, **kwargs)


class RecordingTurn:
    async def next(self, tool_results=(), *, on_text_delta=None):
        if on_text_delta:
            on_text_delta("测试成功")
        return ModelStep(200, "测试成功", (), TokenUsage(4, 3, 7))


def test_web_settings_update_next_production_request_without_env_model_key(tmp_path, monkeypatch):
    store = ModelConfigStore(tmp_path / "model-config.json")
    used_keys = []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "obsolete-env-key")
    monkeypatch.setattr(web_service_module, "ModelConfigStore", lambda: store)
    monkeypatch.setattr(web_service_module, "ModelSelectionStore", lambda: ModelSelectionStore(tmp_path / "selection.json"))
    monkeypatch.setattr(web_service_module, "ContextSettingsStore", lambda: ContextSettingsStore(tmp_path / "context.json"))
    monkeypatch.setattr(web_service_module, "PersonalizationStore", lambda: PersonalizationStore(tmp_path / "personalization.json"))
    monkeypatch.setattr(web_service_module, "DEFAULT_WEB_PROJECTS_PATH", tmp_path / "projects.json")
    monkeypatch.setattr(web_service_module, "DEFAULT_WEB_SESSIONS_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(web_service_module, "DEFAULT_WEB_SESSION_PATH", tmp_path / "legacy.json")
    monkeypatch.setattr(web_service_module, "DEFAULT_WEB_STATISTICS_PATH", tmp_path / "statistics.json")
    monkeypatch.setattr(web_service_module, "create_provider", lambda values: RecordingProvider(values["DEEPSEEK_API_KEY"], values["DEEPSEEK_MODEL"], used_keys))
    monkeypatch.setattr(web_service_module, "create_provider_for_model", lambda name, model, values: RecordingProvider(values["DEEPSEEK_API_KEY"], model, used_keys))

    service = WebUiService.production(tmp_path)
    app = FastAPI()
    app.include_router(create_webui_router(service))
    client = TestClient(app)
    page = client.get("/ui").text
    assert 'id="model-config-form"' in page
    assert 'src="/ui/model-config.js"' in page
    assert "ModelConfigView" in client.get("/ui/model-config.js").text
    assert service.runtime_info.api_key_configured is False
    assert service.session._provider.key == ""

    payload = {
        "expected_revision": 0, "provider": "deepseek", "models": ["custom-model"],
        "api_key_action": "set", "api_key": "saved-secret",
    }
    assert client.put("/ui/api/model-config", json=payload, headers={"Origin": "https://other.example"}).status_code == 403
    response = client.put("/ui/api/model-config", json=payload, headers={"Origin": "http://testserver"})
    assert response.status_code == 200
    assert "saved-secret" not in response.text
    assert response.json()["runtime"]["api_key_configured"] is True
    assert "saved-secret" not in client.get("/ui/api/model-config").text

    events = asyncio.run(_collect(service, "下一轮请求"))
    assert events[-1]["type"] == "completed"
    assert used_keys == ["saved-secret"]
    assert service.session._provider.model == "custom-model"

    restarted_values = store.load().environment()
    tui = build_tui_dependencies(
        system_prompt=None, system_prompt_error=None,
        workspace_registry=None, workspace_error=None,
        skills_count=0, skills_error=None, skill_runtime=None,
        model_selection_store=ModelSelectionStore(tmp_path / "selection.json"),
        model_options=store.load().options(),
        initial_provider=RecordingProvider(restarted_values["DEEPSEEK_API_KEY"], restarted_values["DEEPSEEK_MODEL"], used_keys),
        environ=restarted_values,
    )
    assert tui.chat_session._provider.key == "saved-secret"
    assert tui.runtime_info.api_key_configured is True
    assert [(item.provider, item.model) for item in tui.model_selection.options if item.provider == "deepseek"] == [("deepseek", "custom-model")]

    assert client.put("/ui/api/model-config", json={**payload, "expected_revision": 0}, headers={"Origin": "http://testserver"}).status_code == 409
    assert client.put("/ui/api/model-config", json={**payload, "expected_revision": 1, "api_key": "bad\nkey"}, headers={"Origin": "http://testserver"}).status_code == 422
    cleared = client.put("/ui/api/model-config", json={
        "expected_revision": 1, "provider": "deepseek", "models": ["custom-model"],
        "api_key_action": "clear",
    }, headers={"Origin": "http://testserver"})
    assert cleared.status_code == 200
    assert cleared.json()["runtime"]["api_key_configured"] is False
    assert client.post("/ui/api/chat", json={"input": "不应联网"}).status_code == 503


async def _collect(service, text):
    return [event async for event in service.stream_message(text)]
