"""会话请求事务、上下文压缩与持久化编排。"""

import asyncio
import os
import time
from contextlib import AsyncExitStack
from collections.abc import Callable
from dataclasses import dataclass

from app.observability.model_logging import log_context_management, log_model_token_usage, new_request_id
from app.runtime.chat import ChatErrorCode, ChatResult, ChatRuntimeError, run_chat_messages
from app.runtime.context_compaction import (
    ContextEventHandler,
    PreparedContext,
    SummaryCallResult,
    SummaryCallback,
    _estimate_business,
    prepare_context,
)
from app.runtime.context_settings_store import ContextSettingsError
from app.runtime.memory import ConversationState, ConversationSummary, UserPreference
from app.runtime.model_budget import ModelBudget, ModelBudgetCatalog
from app.runtime.session_store import SessionStore, SessionStoreError
from app.runtime.tool_loop import DEFAULT_TOOL_LOOP_LIMITS, ToolLoopLimits
from app.runtime.trace import TraceObserver
from app.services.llm.contracts import (
    ChatMessage,
    ChatRole,
    GenerationOptions,
    LlmProvider,
    LlmProviderError,
    ProviderContextLimitError,
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


@dataclass(frozen=True)
class RuntimeBudgetSnapshot:
    """请求开始时冻结的模型预算和设置版本。"""

    budget: ModelBudget
    revision: int = 0
    source: str = "default"
    warning: str | None = None


ExecutionSnapshotProvider = Callable[[str], ChatExecutionSnapshot]
BudgetSnapshotProvider = Callable[[LlmProvider], RuntimeBudgetSnapshot]


def _default_budget_snapshot(provider: LlmProvider) -> RuntimeBudgetSnapshot:
    if provider.name in {"deepseek", "aliyun"}:
        budget, sources, _ = ModelBudgetCatalog(os.environ).resolve(provider.name, provider.model)
        source = ",".join(sorted(set(sources.values())))
        return RuntimeBudgetSnapshot(budget, source=source)
    return RuntimeBudgetSnapshot(ModelBudget())


class ChatSession:
    """候选压缩状态只在业务调用和原子保存均成功后提交。"""

    def __init__(
        self,
        store: SessionStore,
        provider: LlmProvider | None = None,
        messages: tuple[ChatMessage, ...] = (),
        system_prompt: str | None = None,
        registry: ToolRuntime | None = None,
        execution_snapshot_provider: ExecutionSnapshotProvider | None = None,
        tool_loop_limits: ToolLoopLimits = DEFAULT_TOOL_LOOP_LIMITS,
        budget_snapshot_provider: BudgetSnapshotProvider = _default_budget_snapshot,
        memory_summarizer: SummaryCallback | None = None,
        summary: ConversationSummary | None = None,
        summary_through_message_count: int = 0,
        context_start_message_count: int = 0,
        preferences: tuple[UserPreference, ...] = (),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if system_prompt is not None and (not isinstance(system_prompt, str) or not system_prompt.strip()):
            raise ValueError("system_prompt must be nonblank text")
        self._store = store
        self._provider = provider
        self._messages = messages
        self._system_prompt = system_prompt
        self._registry = registry
        self._execution_snapshot_provider = execution_snapshot_provider
        self._tool_loop_limits = tool_loop_limits
        self._budget_snapshot_provider = budget_snapshot_provider
        self._memory_summarizer = memory_summarizer or self._summarize_with_provider
        self._summary = summary
        self._summary_through = summary_through_message_count
        self._context_start = context_start_message_count
        self._preferences = preferences
        self._clock = clock
        self._retry_after = 0.0
        self._retry_model: tuple[str, str] | None = None
        self._active_budget: ModelBudget | None = None
        self._active_summary_provider: LlmProvider | None = None
        self._send_lock = asyncio.Lock()
        self._last_context: dict[str, object] | None = None

    @classmethod
    def load(cls, store: SessionStore, **arguments) -> "ChatSession":
        try:
            state = store.load_state()
        except SessionStoreError as exc:
            raise _storage_error(exc) from exc
        return cls(
            store=store,
            messages=state.messages,
            summary=state.summary,
            summary_through_message_count=state.summary_through_message_count,
            context_start_message_count=state.context_start_message_count,
            preferences=state.preferences,
            **arguments,
        )

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        return self._messages

    @property
    def system_prompt_loaded(self) -> bool:
        return self._system_prompt is not None

    @property
    def preferences(self) -> tuple[UserPreference, ...]:
        return self._preferences

    @property
    def summary(self) -> ConversationSummary | None:
        return self._summary

    @property
    def context_start_message_count(self) -> int:
        return self._context_start

    @property
    def context_snapshot(self) -> dict[str, object] | None:
        return dict(self._last_context) if self._last_context is not None else None

    def preview_context_snapshot(self) -> dict[str, object]:
        """恢复或切换后纯本地估算下一次空输入的有效上下文。"""

        provider = self._provider
        if provider is None:
            from app.services.llm.factory import create_provider
            provider = create_provider()
        budget_snapshot = self._budget_snapshot_provider(provider)
        if self._last_context is not None and self._last_context["settings_revision"] == budget_snapshot.revision:
            return dict(self._last_context)
        execution = (
            self._execution_snapshot_provider("") if self._execution_snapshot_provider is not None
            else ChatExecutionSnapshot(self._system_prompt, self._registry)
        )
        state = ConversationState(
            self._messages, self._summary, self._summary_through,
            self._context_start, self._preferences,
        )
        estimate, _ = _estimate_business(
            provider,
            execution.registry.definitions if execution.registry is not None else (),
            budget_snapshot.budget, state, "", execution.system_prompt,
            state.summary, state.preferences, self._summary_through, self._context_start,
        )
        return self._context_payload(estimate, budget_snapshot, scope="restored")

    async def send(
        self,
        input_text: str,
        *,
        on_text_delta: TextDeltaHandler | None = None,
        on_text_reset: TextResetHandler | None = None,
        on_tool_approval: ToolApprovalHandler | None = None,
        on_tool_result: ToolResultHandler | None = None,
        on_context_event: ContextEventHandler | None = None,
        trace_observer: TraceObserver | None = None,
    ) -> ChatResult:
        if not isinstance(input_text, str) or not input_text.strip():
            raise ChatRuntimeError(ChatErrorCode.INVALID_INPUT, "Input must not be blank")
        async with self._send_lock:
            request_id = new_request_id()
            snapshot = (
                self._execution_snapshot_provider(input_text)
                if self._execution_snapshot_provider is not None
                else ChatExecutionSnapshot(self._system_prompt, self._registry)
            )
            async with AsyncExitStack() as resources:
                if snapshot.registry is not None and hasattr(snapshot.registry, "__aenter__"):
                    if hasattr(snapshot.registry, "request_id"):
                        snapshot.registry.request_id = request_id
                    await resources.enter_async_context(snapshot.registry)
                    if on_context_event is not None and getattr(snapshot.registry, "unavailable_servers", ()):
                        on_context_event({
                            "type": "mcp_warning",
                            "message": "MCP Server 暂不可用，本轮已跳过：" + "、".join(snapshot.registry.unavailable_servers),
                        })
                provider = self._provider
                if provider is None:
                    from app.services.llm.factory import create_provider
                    provider = create_provider()
                budget_snapshot = self._budget_snapshot_provider(provider)
                budget = budget_snapshot.budget
                if budget_snapshot.warning is not None and on_context_event is not None:
                    on_context_event({"type": "context_settings_warning", "message": budget_snapshot.warning})
                if self._active_budget is not None and self._active_budget != budget:
                    self._retry_after = 0.0
                    self._retry_model = None
                self._active_budget = budget
                model_key = (provider.name, provider.model)
                retry_allowed = self._retry_model != model_key or self._clock() >= self._retry_after
                state = ConversationState(
                    self._messages, self._summary, self._summary_through,
                    self._context_start, self._preferences,
                )
                self._active_summary_provider = provider
                def report_context(event: dict[str, object]) -> None:
                    if event["type"] == "context_compaction_started":
                        log_context_management(
                            request_id=request_id, phase="summary", outcome="started",
                            before_input_tokens=event["before_input_tokens"],
                            input_limit=budget.input_limit,
                        )
                    elif event["type"] == "context_compaction_finished":
                        log_context_management(
                            request_id=request_id, phase="summary",
                            outcome=str(event["outcome"]), reason=event["reason"],
                            before_input_tokens=event["before_input_tokens"],
                            after_input_tokens=event["after_input_tokens"],
                            input_limit=budget.input_limit,
                            summary_turns=event["summarized_turns"],
                            omitted_turns=event["omitted_turns"],
                            duration_ms=event["duration_ms"],
                        )
                    if on_context_event is not None:
                        on_context_event(event)

                try:
                    prepared = await prepare_context(
                        state, input_text, snapshot.system_prompt, provider,
                        snapshot.registry.definitions if snapshot.registry is not None else (),
                        budget, self._memory_summarizer, request_id=request_id,
                        retry_allowed=retry_allowed, on_context_event=report_context,
                        clock=self._clock,
                    )
                except ValueError as exc:
                    log_context_management(
                        request_id=request_id, phase="budget", outcome="rejected",
                        reason="irreducible", input_limit=budget.input_limit,
                    )
                    raise ChatRuntimeError(
                        ChatErrorCode.CONTEXT_LIMIT,
                        "当前输入超过模型上下文上限，请减少内容、清理记忆或切换更大窗口的模型。",
                    ) from exc
                finally:
                    self._active_summary_provider = None
                if prepared.failure_reason is not None:
                    self._retry_model = model_key
                    self._retry_after = self._clock() + budget.summary_cooldown_seconds
                log_context_management(
                    request_id=request_id, phase="business", outcome=prepared.outcome,
                    reason=prepared.failure_reason,
                    before_input_tokens=prepared.input_tokens,
                    after_input_tokens=prepared.input_tokens, input_limit=budget.input_limit,
                    summary_turns=prepared.summarized_turns,
                    omitted_turns=prepared.omitted_turns,
                )
                self._ensure_not_cancelled()
                user_message = ChatMessage(ChatRole.USER, input_text)
                candidate_request = prepared.context_messages + (user_message,)

                def on_estimate(estimate) -> None:
                    payload = self._context_payload(
                        estimate.input_tokens, budget_snapshot, scope="request",
                    )
                    if on_context_event is not None:
                        on_context_event({"type": "context_updated", "request_id": request_id, **payload})

                result = await run_chat_messages(
                    candidate_request, provider=provider, registry=snapshot.registry,
                    system_prompt=prepared.system_prompt,
                    on_text_delta=on_text_delta, on_text_reset=on_text_reset,
                    on_tool_approval=on_tool_approval, on_tool_result=on_tool_result,
                    tool_loop_limits=self._tool_loop_limits, trace_observer=trace_observer,
                    budget=budget, request_id=request_id, on_context_estimate=on_estimate,
                )
                if prepared.summary_attempted:
                    usage = (
                        prepared.summary_usage + result.token_usage
                        if prepared.summary_usage is not None and result.token_usage is not None
                        else None
                    )
                    result = ChatResult(
                        result.output_text, result.provider, result.model, usage,
                        result.finish_reason,
                    )
                self._ensure_not_cancelled()
                committed = ConversationState(
                    self._messages + (user_message, ChatMessage(ChatRole.ASSISTANT, result.output_text)),
                    prepared.summary, prepared.summary_through_message_count,
                    prepared.context_start_message_count, prepared.preferences,
                )
                next_input_tokens, _ = _estimate_business(
                    provider, snapshot.registry.definitions if snapshot.registry is not None else (),
                    budget, committed, "", snapshot.system_prompt, committed.summary,
                    committed.preferences, committed.summary_through_message_count,
                    committed.context_start_message_count,
                )
                try:
                    self._store.save_state(committed)
                except SessionStoreError as exc:
                    raise _storage_error(exc) from exc
                self._messages = committed.messages
                self._summary = committed.summary
                self._summary_through = committed.summary_through_message_count
                self._context_start = committed.context_start_message_count
                self._preferences = committed.preferences
                self._last_context = self._context_payload(
                    next_input_tokens, budget_snapshot, scope="committed",
                )
                if on_context_event is not None:
                    on_context_event({"type": "context_updated", "request_id": request_id, **self._last_context})
                return result

    def clear(self) -> None:
        try:
            if self._preferences:
                self._store.save_privacy_state(ConversationState(preferences=self._preferences))
            else:
                self._store.clear()
        except SessionStoreError as exc:
            raise _storage_error(exc) from exc
        self._messages = ()
        self._summary = None
        self._summary_through = self._context_start = 0
        self._retry_after = 0
        self._retry_model = None
        self._active_budget = None
        self._last_context = None

    def clear_preferences(self) -> None:
        state = ConversationState(
            self._messages, self._summary, self._summary_through, self._context_start,
        )
        try:
            if state.messages or state.summary:
                self._store.save_privacy_state(state)
            else:
                self._store.clear()
        except SessionStoreError as exc:
            raise _storage_error(exc) from exc
        self._preferences = ()

    def replace_provider(self, provider: LlmProvider) -> None:
        if provider is None:
            raise ValueError("provider is required")
        if self._send_lock.locked():
            raise ChatRuntimeError(ChatErrorCode.CONFIGURATION, "Model cannot be changed while a request is active")
        try:
            self._budget_snapshot_provider(provider)
        except (ContextSettingsError, ValueError) as exc:
            raise ChatRuntimeError(ChatErrorCode.CONFIGURATION, "目标模型的上下文预算不可用。") from exc
        self._provider = provider
        self._retry_after = 0
        self._retry_model = None
        self._active_budget = None
        self._last_context = None

    async def _summarize_with_provider(
        self,
        previous_summary: ConversationSummary | None,
        messages: tuple[ChatMessage, ...],
        budget: ModelBudget,
        parent_request_id: str,
    ) -> SummaryCallResult:
        provider = self._active_summary_provider
        if provider is None:
            raise LlmProviderError("Memory summarization provider is unavailable")
        from app.runtime.memory import SUMMARY_SYSTEM_PROMPT, build_summary_input
        summary_messages = (
            ChatMessage(ChatRole.SYSTEM, SUMMARY_SYSTEM_PROMPT),
            ChatMessage(ChatRole.USER, build_summary_input(previous_summary, messages)),
        )
        child_id = new_request_id()
        log_context_management(
            request_id=child_id, parent_request_id=parent_request_id,
            phase="summary_request", input_limit=budget.summary_input_limit,
        )

        def guard(estimate) -> None:
            if estimate.input_tokens > budget.summary_input_limit:
                log_context_management(
                    request_id=child_id, parent_request_id=parent_request_id,
                    phase="budget", outcome="rejected", reason="summary_input_limit",
                    before_input_tokens=estimate.input_tokens,
                    input_limit=budget.summary_input_limit,
                )
                raise ProviderContextLimitError()

        turn = provider.create_turn(
            summary_messages, (), request_id=child_id,
            options=GenerationOptions(budget.summary_output_tokens), request_guard=guard,
        )
        try:
            step = await turn.next()
        finally:
            await turn.aclose()
        if step.token_usage is not None:
            log_model_token_usage(
                request_id=child_id, step_number=1,
                input_tokens=step.token_usage.input_tokens,
                output_tokens=step.token_usage.output_tokens,
                total_tokens=step.token_usage.total_tokens,
            )
        if step.tool_calls or step.output_text is None or not step.output_text.strip():
            raise LlmProviderError("Memory summarization failed")
        return SummaryCallResult(step.output_text, step.token_usage, step.finish_reason)

    @staticmethod
    def _ensure_not_cancelled() -> None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError()

    @staticmethod
    def _context_payload(
        input_tokens: int, snapshot: RuntimeBudgetSnapshot, *, scope: str,
    ) -> dict[str, object]:
        budget = snapshot.budget
        return {
            "input_tokens": input_tokens,
            "input_limit": budget.input_limit,
            "window_tokens": budget.context_window_tokens,
            "percent": round(input_tokens * 100 / budget.input_limit, 1),
            "budget_source": snapshot.source,
            "settings_revision": snapshot.revision,
            "estimator": "heuristic_v1",
            "scope": scope,
        }


def _storage_error(error: SessionStoreError) -> ChatRuntimeError:
    return ChatRuntimeError(ChatErrorCode.STORAGE, str(error))
