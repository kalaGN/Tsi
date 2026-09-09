import os

from app.runtime.chat import ChatErrorCode, ChatRuntimeError, ChatRuntimeInfo
from app.runtime.model_selection_store import ModelSelection, ModelSelectionStore
from app.runtime.session import ChatSession
from app.runtime.session_store import SessionStore
from app.services.llm.contracts import ModelOption
from app.tui.bootstrap import (
    build_tui_dependencies,
    injected_tui_dependencies,
)


class FakeProvider:
    api_key_configured = True

    def __init__(self, name, model):
        self.name = name
        self.model = model


async def fake_runner(text, **kwargs):
    raise AssertionError("not called")


def test_bootstrap_uses_same_restored_provider_for_info_and_session(tmp_path):
    selection_store = ModelSelectionStore(tmp_path / "model-selection.json")
    selection_store.save(ModelSelection("aliyun", "qwen3-max"))
    provider = FakeProvider("aliyun", "qwen3-max")

    dependencies = build_tui_dependencies(
        system_prompt=None,
        system_prompt_error=None,
        workspace_registry=None,
        workspace_error=None,
        skills_count=0,
        skills_error=None,
        skill_runtime=None,
        model_selection_store=selection_store,
        model_options=(ModelOption("aliyun", "qwen3-max", True),),
        session_store=SessionStore(tmp_path / "chat-session.json"),
        provider_factory=lambda provider_name, model: provider,
        environ={},
    )

    assert dependencies.runtime_info == ChatRuntimeInfo(
        "aliyun", "qwen3-max", True
    )
    assert dependencies.chat_session._provider is provider
    assert dependencies.chat_runner == dependencies.chat_session.send


def test_bootstrap_returns_mountable_dependencies_for_configuration_error(tmp_path):
    def fail_runtime_info():
        raise ChatRuntimeError(
            ChatErrorCode.CONFIGURATION,
            "Unsupported LLM provider configuration",
        )

    dependencies = build_tui_dependencies(
        system_prompt=None,
        system_prompt_error=None,
        workspace_registry=None,
        workspace_error=None,
        skills_count=0,
        skills_error=None,
        skill_runtime=None,
        session_store=SessionStore(tmp_path / "chat-session.json"),
        model_options=(),
        runtime_info_factory=fail_runtime_info,
        environ={},
    )

    issue = dependencies.health.first_blocking_issue
    assert issue.code == "configuration"
    assert issue.message == "Unsupported LLM provider configuration"
    assert dependencies.runtime_info == ChatRuntimeInfo("unknown", "-", False)
    assert dependencies.chat_session is not None


def test_bootstrap_invalid_memory_policy_blocks_without_preventing_session(tmp_path):
    dependencies = build_tui_dependencies(
        system_prompt=None,
        system_prompt_error=None,
        workspace_registry=None,
        workspace_error=None,
        skills_count=0,
        skills_error=None,
        skill_runtime=None,
        session_store=SessionStore(tmp_path / "chat-session.json"),
        model_options=(),
        runtime_info=ChatRuntimeInfo("deepseek", "deepseek-chat", True),
        environ={"TUI_CONTEXT_WINDOW_TOKENS": "invalid"},
    )

    assert "positive integer" in dependencies.health.first_blocking_issue.message
    assert dependencies.chat_session is not None


def test_bootstrap_preserves_corrupt_history_until_explicit_clear(tmp_path):
    path = tmp_path / "chat-session.json"
    path.write_text("not-json", encoding="utf-8")
    store = SessionStore(path)

    dependencies = build_tui_dependencies(
        system_prompt=None,
        system_prompt_error=None,
        workspace_registry=None,
        workspace_error=None,
        skills_count=0,
        skills_error=None,
        skill_runtime=None,
        session_store=store,
        model_options=(),
        runtime_info=ChatRuntimeInfo("deepseek", "deepseek-chat", True),
        environ={},
    )

    assert dependencies.health.first_blocking_issue.code == "history"
    assert path.read_text(encoding="utf-8") == "not-json"
    dependencies.chat_session.clear()
    assert not path.exists()


def test_injected_dependencies_do_not_read_environment_or_disk(monkeypatch):
    monkeypatch.setenv("TUI_CONTEXT_WINDOW_TOKENS", "invalid")

    dependencies = injected_tui_dependencies(
        chat_runner=fake_runner,
        runtime_info=ChatRuntimeInfo("fake", "fake-model", True),
    )

    assert dependencies.chat_runner is fake_runner
    assert dependencies.chat_session is None
    assert dependencies.health.issues == ()
    assert os.environ["TUI_CONTEXT_WINDOW_TOKENS"] == "invalid"


def test_injected_dependencies_use_explicit_session_runner(tmp_path):
    session = ChatSession(SessionStore(tmp_path / "chat-session.json"))

    dependencies = injected_tui_dependencies(
        chat_session=session,
        runtime_info=ChatRuntimeInfo("fake", "fake-model", True),
    )

    assert dependencies.chat_session is session
    assert dependencies.chat_runner == session.send
