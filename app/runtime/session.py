"""将有序模型调用与本地持久化组合为当前会话。"""

import asyncio

from app.runtime.chat import (
    ChatErrorCode,
    ChatResult,
    ChatRuntimeError,
    run_chat_messages,
)
from app.runtime.session_store import SessionStore, SessionStoreError
from app.services.llm.contracts import (
    ChatMessage,
    ChatRole,
    LlmProvider,
    TextDeltaHandler,
    TextResetHandler,
)


class ChatSession:
    """只提交 Provider 调用和持久化都成功的完整轮次。"""

    def __init__(
        self,
        store: SessionStore,
        provider: LlmProvider | None = None,
        messages: tuple[ChatMessage, ...] = (),
        system_prompt: str | None = None,
    ) -> None:
        if system_prompt is not None and (
            not isinstance(system_prompt, str) or not system_prompt.strip()
        ):
            raise ValueError("system_prompt must be nonblank text")
        self._store = store
        self._provider = provider
        self._messages = messages
        self._system_prompt = system_prompt
        self._send_lock = asyncio.Lock()

    @classmethod
    def load(
        cls,
        store: SessionStore,
        provider: LlmProvider | None = None,
        system_prompt: str | None = None,
    ) -> "ChatSession":
        try:
            messages = store.load()
        except SessionStoreError as exc:
            raise _storage_error(exc) from exc
        return cls(
            store=store,
            provider=provider,
            messages=messages,
            system_prompt=system_prompt,
        )

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        return self._messages

    @property
    def system_prompt_loaded(self) -> bool:
        """只暴露加载状态，避免 TUI 或日志意外回显系统提示词。"""

        return self._system_prompt is not None

    async def send(
        self,
        input_text: str,
        *,
        on_text_delta: TextDeltaHandler | None = None,
        on_text_reset: TextResetHandler | None = None,
    ) -> ChatResult:
        """流式执行一轮，并只在完整成功后提交磁盘与内存历史。"""

        if not isinstance(input_text, str) or not input_text.strip():
            raise ChatRuntimeError(
                ChatErrorCode.INVALID_INPUT,
                "Input must not be blank",
            )

        async with self._send_lock:
            user_message = ChatMessage(ChatRole.USER, input_text)
            candidate_request = self._messages + (user_message,)
            result = await run_chat_messages(
                candidate_request,
                provider=self._provider,
                system_prompt=self._system_prompt,
                on_text_delta=on_text_delta,
                on_text_reset=on_text_reset,
            )
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                # 即使底层 Provider 吞掉取消，也不能把用户已取消的轮次落盘。
                raise asyncio.CancelledError()
            committed = candidate_request + (
                ChatMessage(ChatRole.ASSISTANT, result.output_text),
            )
            try:
                self._store.save(committed)
            except SessionStoreError as exc:
                raise _storage_error(exc) from exc
            self._messages = committed
            return result

    def clear(self) -> None:
        try:
            self._store.clear()
        except SessionStoreError as exc:
            raise _storage_error(exc) from exc
        self._messages = ()


def _storage_error(error: SessionStoreError) -> ChatRuntimeError:
    return ChatRuntimeError(ChatErrorCode.STORAGE, str(error))
