import asyncio

import pytest

from app.runtime.chat import ChatErrorCode, ChatRuntimeError
from app.runtime.session import ChatSession
from app.runtime.session_store import SessionStore, SessionStoreError
from app.services.llm.contracts import (
    ChatMessage,
    ChatRole,
    ProviderResult,
    ProviderTimeoutError,
)


class RecordingProvider:
    name = "fake"
    model = "fake-model"
    api_key_configured = True

    def __init__(self, answers=None, error=None):
        self.answers = iter(answers or [])
        self.error = error
        self.calls = []

    async def generate(self, messages):
        self.calls.append(tuple(messages))
        if self.error is not None:
            raise self.error
        answer = next(self.answers)
        return ProviderResult(200, {}, answer)


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
            async def generate(self, messages):
                self.calls.append(tuple(messages))
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
            async def generate(self, messages):
                self.calls.append(tuple(messages))
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return ProviderResult(200, {}, "late answer")

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
