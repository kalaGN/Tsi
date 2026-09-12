"""将有序模型调用与本地持久化组合为当前会话。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from app.runtime.chat import (
    ChatErrorCode,
    ChatResult,
    ChatRuntimeError,
    run_chat_messages,
)
from app.observability.model_logging import log_model_token_usage, new_request_id
from app.runtime.memory import (
    SUMMARY_SYSTEM_PROMPT,
    ConversationState,
    MemoryPolicy,
    SummaryCallback,
    UserPreference,
    build_memory_prompt,
    build_summary_input,
    prepare_memory,
)
from app.runtime.session_store import SessionStore, SessionStoreError
from app.runtime.system_prompt import compose_system_prompt
from app.runtime.tool_loop import DEFAULT_TOOL_LOOP_LIMITS, ToolLoopLimits
from app.runtime.trace import TraceObserver
from app.services.llm.contracts import (
    ChatMessage,
    ChatRole,
    LlmProvider,
    LlmProviderError,
    TextDeltaHandler,
    TextResetHandler,
    TokenUsage,
)
from tools import ToolApprovalHandler, ToolResultHandler, ToolRuntime


@dataclass(frozen=True)
class ChatExecutionSnapshot:
    """一次发送从开始到结束共用的系统提示词与工具快照。"""

    system_prompt: str | None
    registry: ToolRuntime | None
    version: int = 0


ExecutionSnapshotProvider = Callable[[str], ChatExecutionSnapshot]


class ChatSession:
    """只提交 Provider 调用和持久化都成功的完整轮次。"""

    def __init__(
        self,
        store: SessionStore,
        provider: LlmProvider | None = None,
        messages: tuple[ChatMessage, ...] = (),
        system_prompt: str | None = None,
        registry: ToolRuntime | None = None,
        execution_snapshot_provider: ExecutionSnapshotProvider | None = None,
        tool_loop_limits: ToolLoopLimits = DEFAULT_TOOL_LOOP_LIMITS,
        memory_policy: MemoryPolicy = MemoryPolicy(),
        memory_summarizer: SummaryCallback | None = None,
        summary: str | None = None,
        summarized_message_count: int = 0,
        preferences: tuple[UserPreference, ...] = (),
    ) -> None:
        if system_prompt is not None and (
            not isinstance(system_prompt, str) or not system_prompt.strip()
        ):
            raise ValueError("system_prompt must be nonblank text")
        self._store = store
        self._provider = provider
        self._messages = messages
        self._system_prompt = system_prompt
        self._registry = registry
        self._execution_snapshot_provider = execution_snapshot_provider
        self._tool_loop_limits = tool_loop_limits
        self._memory_policy = memory_policy
        self._memory_summarizer = (
            memory_summarizer or self._summarize_with_provider
        )
        self._summary = summary
        self._summarized_message_count = summarized_message_count
        self._preferences = preferences
        self._summary_attempted = False
        self._summary_usage: TokenUsage | None = None
        self._send_lock = asyncio.Lock()

    @classmethod
    def load(
        cls,
        store: SessionStore,
        provider: LlmProvider | None = None,
        system_prompt: str | None = None,
        registry: ToolRuntime | None = None,
        execution_snapshot_provider: ExecutionSnapshotProvider | None = None,
        tool_loop_limits: ToolLoopLimits = DEFAULT_TOOL_LOOP_LIMITS,
        memory_policy: MemoryPolicy = MemoryPolicy(),
        memory_summarizer: SummaryCallback | None = None,
    ) -> "ChatSession":
        try:
            state = store.load_state()
        except SessionStoreError as exc:
            raise _storage_error(exc) from exc
        return cls(
            store=store,
            provider=provider,
            messages=state.messages,
            system_prompt=system_prompt,
            registry=registry,
            execution_snapshot_provider=execution_snapshot_provider,
            tool_loop_limits=tool_loop_limits,
            memory_policy=memory_policy,
            memory_summarizer=memory_summarizer,
            summary=state.summary,
            summarized_message_count=state.summarized_message_count,
            preferences=state.preferences,
        )

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        return self._messages

    @property
    def system_prompt_loaded(self) -> bool:
        """只暴露加载状态，避免 TUI 或日志意外回显系统提示词。"""

        return self._system_prompt is not None

    @property
    def preferences(self) -> tuple[UserPreference, ...]:
        """返回不可变的长期偏好快照，供本地命令安全展示。"""

        return self._preferences

    @property
    def summary(self) -> str | None:
        """暴露摘要存在性与测试边界，不负责在 TUI 直接展示。"""

        return self._summary

    async def send(
        self,
        input_text: str,
        *,
        on_text_delta: TextDeltaHandler | None = None,
        on_text_reset: TextResetHandler | None = None,
        on_tool_approval: ToolApprovalHandler | None = None,
        on_tool_result: ToolResultHandler | None = None,
        trace_observer: TraceObserver | None = None,
    ) -> ChatResult:
        """流式执行一轮，并只在完整成功后提交磁盘与内存历史。"""

        if not isinstance(input_text, str) or not input_text.strip():
            raise ChatRuntimeError(
                ChatErrorCode.INVALID_INPUT,
                "Input must not be blank",
            )

        async with self._send_lock:
            self._summary_attempted = False
            self._summary_usage = None
            snapshot = (
                self._execution_snapshot_provider(input_text)
                if self._execution_snapshot_provider is not None
                else ChatExecutionSnapshot(
                    system_prompt=self._system_prompt,
                    registry=self._registry,
                )
            )
            user_message = ChatMessage(ChatRole.USER, input_text)
            current_state = ConversationState(
                messages=self._messages,
                summary=self._summary,
                summarized_message_count=self._summarized_message_count,
                preferences=self._preferences,
            )
            try:
                prepared = await prepare_memory(
                    current_state,
                    input_text,
                    snapshot.system_prompt,
                    self._memory_summarizer,
                    policy=self._memory_policy,
                )
            except ValueError as exc:
                raise ChatRuntimeError(
                    ChatErrorCode.CONTEXT_LIMIT,
                    "Current input exceeds the context window",
                ) from exc
            candidate_request = prepared.context_messages + (user_message,)
            memory_prompt = build_memory_prompt(
                prepared.summary,
                prepared.preferences,
            )
            result = await run_chat_messages(
                candidate_request,
                provider=self._provider,
                registry=snapshot.registry,
                system_prompt=compose_system_prompt(
                    snapshot.system_prompt,
                    memory_prompt,
                ),
                on_text_delta=on_text_delta,
                on_text_reset=on_text_reset,
                on_tool_approval=on_tool_approval,
                on_tool_result=on_tool_result,
                tool_loop_limits=self._tool_loop_limits,
                trace_observer=trace_observer,
            )
            if self._summary_attempted:
                aggregate_usage = (
                    self._summary_usage + result.token_usage
                    if self._summary_usage is not None
                    and result.token_usage is not None
                    else None
                )
                result = ChatResult(
                    output_text=result.output_text,
                    provider=result.provider,
                    model=result.model,
                    token_usage=aggregate_usage,
                )
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                # 即使底层 Provider 吞掉取消，也不能把用户已取消的轮次落盘。
                raise asyncio.CancelledError()
            committed_messages = self._messages + (
                user_message,
                ChatMessage(ChatRole.ASSISTANT, result.output_text),
            )
            committed = ConversationState(
                messages=committed_messages,
                summary=prepared.summary,
                summarized_message_count=prepared.summarized_message_count,
                preferences=prepared.preferences,
            )
            try:
                self._store.save_state(committed)
            except SessionStoreError as exc:
                raise _storage_error(exc) from exc
            self._messages = committed.messages
            self._summary = committed.summary
            self._summarized_message_count = committed.summarized_message_count
            self._preferences = committed.preferences
            return result

    def clear(self) -> None:
        """清除对话与摘要，但保留用户长期偏好。"""

        try:
            if self._preferences:
                self._store.save_state(
                    ConversationState(preferences=self._preferences)
                )
            else:
                self._store.clear()
        except SessionStoreError as exc:
            raise _storage_error(exc) from exc
        self._messages = ()
        self._summary = None
        self._summarized_message_count = 0

    def clear_preferences(self) -> None:
        """单独清除长期偏好，不影响 Transcript 或摘要。"""

        state = ConversationState(
            messages=self._messages,
            summary=self._summary,
            summarized_message_count=self._summarized_message_count,
        )
        try:
            if state.messages or state.summary:
                self._store.save_state(state)
            else:
                self._store.clear()
        except SessionStoreError as exc:
            raise _storage_error(exc) from exc
        self._preferences = ()

    def replace_provider(self, provider: LlmProvider) -> None:
        """只替换后续请求使用的 Provider，不改变已提交会话。"""

        if provider is None:
            raise ValueError("provider is required")
        # 同步检查与赋值不会让出事件循环，可阻止请求中途观察到新 Provider。
        if self._send_lock.locked():
            raise ChatRuntimeError(
                ChatErrorCode.CONFIGURATION,
                "Model cannot be changed while a request is active",
            )
        self._provider = provider

    async def _summarize_with_provider(
        self,
        previous_summary: str | None,
        messages: tuple[ChatMessage, ...],
    ) -> str:
        """使用当前 Provider 完成一次不开放工具的内部摘要调用。"""

        provider = self._provider
        self._summary_attempted = True
        if provider is None:
            from app.services.llm.factory import create_provider

            provider = create_provider()
        summary_messages = (
            ChatMessage(ChatRole.SYSTEM, SUMMARY_SYSTEM_PROMPT),
            ChatMessage(
                ChatRole.USER,
                build_summary_input(previous_summary, messages),
            ),
        )
        summary_request_id = new_request_id()
        turn = provider.create_turn(
            summary_messages,
            (),
            request_id=summary_request_id,
        )
        step = await turn.next()
        self._summary_usage = step.token_usage
        if step.token_usage is not None:
            log_model_token_usage(
                request_id=summary_request_id,
                step_number=1,
                input_tokens=step.token_usage.input_tokens,
                output_tokens=step.token_usage.output_tokens,
                total_tokens=step.token_usage.total_tokens,
            )
        if step.tool_calls or step.output_text is None or not step.output_text.strip():
            raise LlmProviderError("Memory summarization failed")
        return step.output_text


def _storage_error(error: SessionStoreError) -> ChatRuntimeError:
    return ChatRuntimeError(ChatErrorCode.STORAGE, str(error))
