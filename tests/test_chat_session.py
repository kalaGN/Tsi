import asyncio

import pytest

from app.runtime.chat import ChatErrorCode, ChatRuntimeError
from app.runtime.session import ChatSession
from app.runtime.session_store import SessionStore, SessionStoreError
from app.services.llm.contracts import (
    ChatMessage,
    ChatRole,
    ModelStep,
    ProviderTimeoutError,
)
from tools.contracts import ToolCall


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

        monkeypatch.setattr(store, "save", fail_save)

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
        session = ChatSession(store, provider=provider)

        await session.send("what time is it?")

        assert len(provider.tool_results) == 2
        assert provider.tool_results[0] == ()
        assert provider.tool_results[1][0].call_id == "time-call"
        assert session.messages == (
            ChatMessage(ChatRole.USER, "what time is it?"),
            ChatMessage(ChatRole.ASSISTANT, "final answer"),
        )
        assert SessionStore(store.path).load() == session.messages

    asyncio.run(scenario())
