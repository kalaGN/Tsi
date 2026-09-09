import asyncio

import pytest

from app.runtime import session as session_module
from app.runtime.chat import ChatErrorCode, ChatResult, ChatRuntimeError
from app.runtime.memory import ConversationState, MemoryPolicy
from app.runtime.session import ChatExecutionSnapshot, ChatSession
from app.runtime.session_store import SessionStore, SessionStoreError
from app.runtime.skill_runtime import SkillRuntime
from app.runtime.tool_loop import WORKSPACE_TOOL_LOOP_LIMITS
from app.services.llm.contracts import (
    ChatMessage,
    ChatRole,
    ModelStep,
    ProviderTimeoutError,
    TokenUsage,
)
from tools import create_default_registry
from tools.contracts import ToolCall
from tools.skills import load_skill_catalog
from tools.workspace import WorkspacePolicy


class RecordingProvider:
    name = "fake"
    model = "fake-model"
    api_key_configured = True

    def __init__(self, answers=None, error=None):
        self.answers = iter(answers or [])
        self.error = error
        self.calls = []

    def create_turn(self, messages, tools, *, request_id):
        self.calls.append(tuple(messages))
        return RecordingTurn(self)

    async def next_step(self, tool_results=(), *, on_text_delta=None):
        if self.error is not None:
            raise self.error
        answer = next(self.answers)
        if on_text_delta is not None:
            on_text_delta(answer)
        return ModelStep(200, answer, ())


class RecordingTurn:
    def __init__(self, provider):
        self.provider = provider

    async def next(self, tool_results=(), *, on_text_delta=None):
        return await self.provider.next_step(
            tool_results,
            on_text_delta=on_text_delta,
        )


def test_chat_session_persists_turns_and_restores_history(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        provider = RecordingProvider(["第一答", "第二答"])
        session = ChatSession.load(store, provider=provider)

        await session.send("第一问")
        await session.send("第二问")

        assert provider.calls == [
            (ChatMessage(ChatRole.USER, "第一问"),),
            (
                ChatMessage(ChatRole.USER, "第一问"),
                ChatMessage(ChatRole.ASSISTANT, "第一答"),
                ChatMessage(ChatRole.USER, "第二问"),
            ),
        ]
        restored = ChatSession.load(store)
        assert restored.messages == session.messages

    asyncio.run(scenario())


def test_chat_session_replaces_provider_without_changing_committed_history(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        first = RecordingProvider(["第一答"])
        second = RecordingProvider(["第二答"])
        session = ChatSession(store, provider=first)

        await session.send("第一问")
        committed_before_switch = session.messages
        session.replace_provider(second)

        assert session.messages == committed_before_switch
        assert store.load() == committed_before_switch

        await session.send("第二问")

        assert len(first.calls) == 1
        assert second.calls == [
            committed_before_switch + (ChatMessage(ChatRole.USER, "第二问"),)
        ]

    asyncio.run(scenario())


def test_chat_session_rejects_provider_replacement_while_sending(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        observed_providers = []

        async def blocking_run(_messages, **kwargs):
            observed_providers.append(kwargs["provider"])
            started.set()
            await release.wait()
            return ChatResult("完成", "fake", "fake-model")

        monkeypatch.setattr(session_module, "run_chat_messages", blocking_run)
        original = RecordingProvider()
        replacement = RecordingProvider()
        session = ChatSession(
            SessionStore(tmp_path / "chat-session.json"),
            provider=original,
        )
        send_task = asyncio.create_task(session.send("问题"))
        await started.wait()

        with pytest.raises(ChatRuntimeError) as captured:
            session.replace_provider(replacement)

        assert captured.value.code is ChatErrorCode.CONFIGURATION
        release.set()
        await send_task
        await session.send("第二问")
        assert observed_providers == [original, original]

    asyncio.run(scenario())


def test_chat_session_uses_system_prompt_without_persisting_it(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        provider = RecordingProvider(["第一答", "第二答"])
        session = ChatSession.load(
            store,
            provider=provider,
            system_prompt="项目系统规则",
        )

        await session.send("第一问")
        session.clear()
        await session.send("第二问")

        assert provider.calls == [
            (
                ChatMessage(ChatRole.SYSTEM, "项目系统规则"),
                ChatMessage(ChatRole.USER, "第一问"),
            ),
            (
                ChatMessage(ChatRole.SYSTEM, "项目系统规则"),
                ChatMessage(ChatRole.USER, "第二问"),
            ),
        ]
        assert session.system_prompt_loaded is True
        assert session.messages == (
            ChatMessage(ChatRole.USER, "第二问"),
            ChatMessage(ChatRole.ASSISTANT, "第二答"),
        )
        assert SessionStore(store.path).load() == session.messages

    asyncio.run(scenario())


def test_restored_session_uses_current_startup_system_prompt(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        store.save(
            (
                ChatMessage(ChatRole.USER, "old question"),
                ChatMessage(ChatRole.ASSISTANT, "old answer"),
            )
        )
        provider = RecordingProvider(["new answer"])
        session = ChatSession.load(
            store,
            provider=provider,
            system_prompt="new startup rules",
        )

        await session.send("new question")

        assert provider.calls[0] == (
            ChatMessage(ChatRole.SYSTEM, "new startup rules"),
            ChatMessage(ChatRole.USER, "old question"),
            ChatMessage(ChatRole.ASSISTANT, "old answer"),
            ChatMessage(ChatRole.USER, "new question"),
        )

    asyncio.run(scenario())


def test_chat_session_reads_one_execution_snapshot_per_send(tmp_path):
    async def scenario():
        provider = RecordingProvider(["第一答", "第二答"])
        prompts = iter(("规则一", "规则二"))
        snapshot_calls = []

        def snapshot_provider(input_text):
            prompt = next(prompts)
            snapshot_calls.append((input_text, prompt))
            return ChatExecutionSnapshot(
                system_prompt=prompt,
                registry=create_default_registry(),
                version=len(snapshot_calls),
            )

        session = ChatSession(
            SessionStore(tmp_path / "chat-session.json"),
            provider=provider,
            execution_snapshot_provider=snapshot_provider,
        )

        await session.send("第一问")
        await session.send("第二问")

        assert snapshot_calls == [("第一问", "规则一"), ("第二问", "规则二")]
        assert provider.calls[0][0] == ChatMessage(ChatRole.SYSTEM, "规则一")
        assert provider.calls[1][0] == ChatMessage(ChatRole.SYSTEM, "规则二")

    asyncio.run(scenario())


def test_chat_session_loads_explicit_skill_without_persisting_its_body(tmp_path):
    async def scenario():
        skill_root = tmp_path / ".agents/skills/demo-skill"
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: demo\n---\n\nPRIVATE BODY\n",
            encoding="utf-8",
        )
        runtime = SkillRuntime(
            tmp_path,
            None,
            WorkspacePolicy(tmp_path),
            load_skill_catalog(tmp_path),
            codex_skills_root=tmp_path / "codex-skills",
        )
        store = SessionStore(tmp_path / "chat-session.json")
        provider = RecordingProvider(["完成"])
        session = ChatSession(
            store,
            provider=provider,
            execution_snapshot_provider=runtime.snapshot,
        )

        await session.send("请用 $demo-skill 处理")

        assert "PRIVATE BODY" in provider.calls[0][0].content
        assert SessionStore(store.path).load() == (
            ChatMessage(ChatRole.USER, "请用 $demo-skill 处理"),
            ChatMessage(ChatRole.ASSISTANT, "完成"),
        )

    asyncio.run(scenario())


def test_chat_session_forwards_deltas_before_persisting_complete_turn(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        session = ChatSession(store, provider=RecordingProvider(["完整回答"]))
        deltas = []

        result = await session.send("问题", on_text_delta=deltas.append)

        assert deltas == ["完整回答"]
        assert result.output_text == "完整回答"
        assert SessionStore(store.path).load() == (
            ChatMessage(ChatRole.USER, "问题"),
            ChatMessage(ChatRole.ASSISTANT, "完整回答"),
        )

    asyncio.run(scenario())


def test_chat_session_forwards_registry_limits_and_tool_callbacks(tmp_path, monkeypatch):
    async def scenario():
        captured = {}
        registry = create_default_registry()

        async def fake_run(messages, **kwargs):
            captured["messages"] = messages
            captured.update(kwargs)
            return ChatResult("answer", "fake", "fake-model")

        async def approve(_request):
            return True

        observed = lambda call, result: None
        monkeypatch.setattr(session_module, "run_chat_messages", fake_run)
        session = ChatSession(
            SessionStore(tmp_path / "session.json"),
            registry=registry,
            tool_loop_limits=WORKSPACE_TOOL_LOOP_LIMITS,
        )

        await session.send(
            "question",
            on_tool_approval=approve,
            on_tool_result=observed,
        )

        assert captured["registry"] is registry
        assert captured["tool_loop_limits"] is WORKSPACE_TOOL_LOOP_LIMITS
        assert captured["on_tool_approval"] is approve
        assert captured["on_tool_result"] is observed

    asyncio.run(scenario())


def test_chat_session_does_not_commit_provider_failure(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        session = ChatSession(
            store,
            provider=RecordingProvider(error=ProviderTimeoutError()),
        )

        with pytest.raises(ChatRuntimeError):
            await session.send("hello")

        assert session.messages == ()
        assert not store.path.exists()

    asyncio.run(scenario())


def test_chat_session_does_not_commit_when_persistence_fails(tmp_path, monkeypatch):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        session = ChatSession(store, provider=RecordingProvider(["answer"]))

        def fail_save(messages):
            raise SessionStoreError("Unable to save conversation")

        monkeypatch.setattr(store, "save_state", fail_save)

        with pytest.raises(ChatRuntimeError) as captured:
            await session.send("hello")

        assert captured.value.code is ChatErrorCode.STORAGE
        assert session.messages == ()

    asyncio.run(scenario())


def test_chat_session_cancellation_does_not_commit(tmp_path):
    async def scenario():
        started = asyncio.Event()

        class BlockingProvider(RecordingProvider):
            async def next_step(self, tool_results=(), *, on_text_delta=None):
                started.set()
                await asyncio.Event().wait()

        store = SessionStore(tmp_path / "chat-session.json")
        session = ChatSession(store, provider=BlockingProvider())
        task = asyncio.create_task(session.send("hello"))
        await started.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert session.messages == ()
        assert not store.path.exists()

    asyncio.run(scenario())


def test_chat_session_does_not_commit_when_provider_swallows_cancellation(
    tmp_path,
):
    async def scenario():
        started = asyncio.Event()

        class CancellationSwallowingProvider(RecordingProvider):
            async def next_step(self, tool_results=(), *, on_text_delta=None):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return ModelStep(200, "late answer", ())

        store = SessionStore(tmp_path / "chat-session.json")
        session = ChatSession(
            store,
            provider=CancellationSwallowingProvider(),
        )
        task = asyncio.create_task(session.send("hello"))
        await started.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert session.messages == ()
        assert not store.path.exists()

    asyncio.run(scenario())


def test_chat_session_serializes_concurrent_turns(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        provider = RecordingProvider(["answer-1", "answer-2"])
        session = ChatSession(store, provider=provider)

        await asyncio.gather(session.send("first"), session.send("second"))

        assert provider.calls[0] == (ChatMessage(ChatRole.USER, "first"),)
        assert provider.calls[1][-1] == ChatMessage(ChatRole.USER, "second")
        assert provider.calls[1][1] == ChatMessage(
            ChatRole.ASSISTANT,
            "answer-1",
        )

    asyncio.run(scenario())


def test_chat_session_clear_removes_memory_and_file(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        session = ChatSession(store, provider=RecordingProvider(["answer"]))
        await session.send("hello")

        session.clear()

        assert session.messages == ()
        assert not store.path.exists()

    asyncio.run(scenario())


def test_chat_session_persists_only_final_turn_after_automatic_tool_call(tmp_path):
    async def scenario():
        class ToolCallingProvider(RecordingProvider):
            def __init__(self):
                super().__init__()
                self.steps = iter(
                    [
                        ModelStep(
                            200,
                            None,
                            (
                                ToolCall(
                                    "time-call",
                                    "get_current_time",
                                    '{"timezone":"UTC"}',
                                ),
                            ),
                        ),
                        ModelStep(200, "final answer", ()),
                    ]
                )
                self.tool_results = []

            async def next_step(self, tool_results=(), *, on_text_delta=None):
                self.tool_results.append(tuple(tool_results))
                step = next(self.steps)
                if on_text_delta is not None and step.output_text:
                    on_text_delta(step.output_text)
                return step

        store = SessionStore(tmp_path / "chat-session.json")
        provider = ToolCallingProvider()
        session = ChatSession(
            store,
            provider=provider,
            system_prompt="project rules",
        )

        await session.send("what time is it?")

        assert provider.calls == [
            (
                ChatMessage(ChatRole.SYSTEM, "project rules"),
                ChatMessage(ChatRole.USER, "what time is it?"),
            )
        ]
        assert len(provider.tool_results) == 2
        assert provider.tool_results[0] == ()
        assert provider.tool_results[1][0].call_id == "time-call"
        assert session.messages == (
            ChatMessage(ChatRole.USER, "what time is it?"),
            ChatMessage(ChatRole.ASSISTANT, "final answer"),
        )
        assert SessionStore(store.path).load() == session.messages

    asyncio.run(scenario())


def test_chat_session_compacts_model_context_but_preserves_full_transcript(tmp_path):
    async def scenario():
        history = tuple(
            message
            for number in range(8)
            for message in (
                ChatMessage(ChatRole.USER, f"问题{number}" + "中" * 30),
                ChatMessage(ChatRole.ASSISTANT, f"回答{number}" + "文" * 30),
            )
        )
        store = SessionStore(tmp_path / "chat-session.json")
        store.save_state(ConversationState(messages=history))
        provider = RecordingProvider(["本轮回答"])
        summary_calls = []

        async def summarize(previous, messages):
            summary_calls.append((previous, messages))
            return "此前摘要"

        session = ChatSession.load(
            store,
            provider=provider,
            memory_summarizer=summarize,
            memory_policy=MemoryPolicy(
                context_window_tokens=260,
                trigger_ratio=0.7,
                target_ratio=0.6,
                recent_turns=2,
                reserved_tokens=20,
            ),
        )

        await session.send("继续开发")

        assert summary_calls
        assert provider.calls[0][0].role is ChatRole.SYSTEM
        assert "此前摘要" in provider.calls[0][0].content
        assert provider.calls[0][-1] == ChatMessage(ChatRole.USER, "继续开发")
        assert len(provider.calls[0]) < len(history) + 2
        assert session.messages == history + (
            ChatMessage(ChatRole.USER, "继续开发"),
            ChatMessage(ChatRole.ASSISTANT, "本轮回答"),
        )
        persisted = store.load_state()
        assert persisted.messages == session.messages
        assert persisted.summary == "此前摘要"
        assert persisted.summarized_message_count > 0

    asyncio.run(scenario())


def test_default_memory_summarizer_uses_current_provider_without_tools(tmp_path):
    async def scenario():
        class SummaryProvider(RecordingProvider):
            def __init__(self):
                super().__init__(["模型摘要", "业务回答"])
                self.tools = []

            def create_turn(self, messages, tools, *, request_id):
                self.calls.append(tuple(messages))
                self.tools.append(tuple(tools))
                return RecordingTurn(self)

        history = tuple(
            message
            for number in range(5)
            for message in _history_turn(number, 30)
        )
        store = SessionStore(tmp_path / "chat-session.json")
        store.save_state(ConversationState(messages=history))
        provider = SummaryProvider()
        session = ChatSession.load(
            store,
            provider=provider,
            memory_policy=MemoryPolicy(
                context_window_tokens=190,
                trigger_ratio=0.7,
                target_ratio=0.6,
                recent_turns=1,
                reserved_tokens=20,
            ),
        )

        await session.send("继续")

        assert provider.tools[0] == ()
        assert "会话记忆压缩器" in provider.calls[0][0].content
        assert provider.calls[1][-1] == ChatMessage(ChatRole.USER, "继续")
        assert session.summary == "模型摘要"

    def _history_turn(number, size):
        return (
            ChatMessage(ChatRole.USER, f"问{number}" + "中" * size),
            ChatMessage(ChatRole.ASSISTANT, f"答{number}" + "文" * size),
        )

    asyncio.run(scenario())


def test_default_memory_summarizer_usage_is_included_in_user_turn_total(tmp_path):
    async def scenario():
        class UsageProvider(RecordingProvider):
            def __init__(self):
                super().__init__()
                self.steps = iter(
                    (
                        ModelStep(200, "摘要", (), TokenUsage(10, 2, 12)),
                        ModelStep(200, "回答", (), TokenUsage(20, 3, 23)),
                    )
                )

            async def next_step(self, tool_results=(), *, on_text_delta=None):
                step = next(self.steps)
                if on_text_delta is not None and step.output_text:
                    on_text_delta(step.output_text)
                return step

        history = tuple(
            message
            for number in range(5)
            for message in (
                ChatMessage(ChatRole.USER, f"问{number}" + "中" * 30),
                ChatMessage(ChatRole.ASSISTANT, f"答{number}" + "文" * 30),
            )
        )
        store = SessionStore(tmp_path / "chat-session.json")
        store.save_state(ConversationState(messages=history))
        session = ChatSession.load(
            store,
            provider=UsageProvider(),
            memory_policy=MemoryPolicy(
                context_window_tokens=190,
                trigger_ratio=0.7,
                target_ratio=0.6,
                recent_turns=1,
                reserved_tokens=20,
            ),
        )

        result = await session.send("继续")

        assert result.token_usage == TokenUsage(30, 5, 35)

    asyncio.run(scenario())


def test_explicit_preference_is_injected_and_survives_clear(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        provider = RecordingProvider(["好的"])
        session = ChatSession(store, provider=provider)

        await session.send("以后请使用中文回复")

        assert "使用中文回复" in provider.calls[0][0].content
        assert [item.content for item in session.preferences] == ["使用中文回复"]
        session.clear()
        assert session.messages == ()
        assert [item.content for item in session.preferences] == ["使用中文回复"]
        restored = ChatSession.load(store)
        assert restored.messages == ()
        assert [item.content for item in restored.preferences] == ["使用中文回复"]

        restored.clear_preferences()
        assert restored.preferences == ()
        assert not store.path.exists()

    asyncio.run(scenario())


def test_context_limit_does_not_call_provider_or_commit(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        provider = RecordingProvider(["不应使用"])
        session = ChatSession(
            store,
            provider=provider,
            memory_policy=MemoryPolicy(
                context_window_tokens=80,
                trigger_ratio=0.7,
                target_ratio=0.5,
                recent_turns=0,
                reserved_tokens=10,
            ),
        )

        with pytest.raises(ChatRuntimeError) as captured:
            await session.send("中" * 100)

        assert captured.value.code is ChatErrorCode.CONTEXT_LIMIT
        assert provider.calls == []
        assert session.messages == ()
        assert not store.path.exists()

    asyncio.run(scenario())
