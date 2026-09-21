"""在真实 Provider 投影上选择摘要批次和可见历史边界。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Sequence

from app.runtime.memory import (
    ConversationState,
    ConversationSummary,
    UserPreference,
    build_memory_prompt,
    build_summary_input,
    extract_explicit_preferences,
    parse_conversation_summary,
    SUMMARY_SYSTEM_PROMPT,
)
from app.runtime.model_budget import ModelBudget
from app.runtime.system_prompt import compose_system_prompt
from app.services.llm.contracts import (
    ChatMessage,
    ChatRole,
    GenerationOptions,
    LlmProvider,
    LlmProviderError,
    TokenUsage,
)
from tools.contracts import ToolDefinition


SUMMARY_FAILURE_REASONS = {
    "timeout", "provider_error", "invalid_summary", "output_limit", "batch_too_large",
}


@dataclass(frozen=True)
class SummaryCallResult:
    output_text: str
    token_usage: TokenUsage | None
    finish_reason: str | None


SummaryCallback = Callable[
    [ConversationSummary | None, tuple[ChatMessage, ...], ModelBudget, str],
    Awaitable[SummaryCallResult],
]
ContextEventHandler = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class PreparedContext:
    """业务成功后才允许提交的上下文候选。"""

    context_messages: tuple[ChatMessage, ...]
    summary: ConversationSummary | None
    summary_through_message_count: int
    context_start_message_count: int
    preferences: tuple[UserPreference, ...]
    system_prompt: str | None
    input_tokens: int
    summary_usage: TokenUsage | None
    summary_attempted: bool
    failure_reason: str | None
    summarized_turns: int
    omitted_turns: int
    outcome: str


def _provider_messages(
    state: ConversationState,
    current_input: str,
    base_system_prompt: str | None,
    summary: ConversationSummary | None,
    preferences: Sequence[UserPreference],
    summary_through: int,
    context_start: int,
) -> tuple[ChatMessage, ...]:
    memory_prompt = build_memory_prompt(
        summary, preferences, omitted_turns=(context_start - summary_through) // 2,
    )
    system = compose_system_prompt(base_system_prompt, memory_prompt)
    prefix = (ChatMessage(ChatRole.SYSTEM, system),) if system is not None else ()
    return prefix + state.messages[context_start:] + (ChatMessage(ChatRole.USER, current_input),)


def _estimate_business(
    provider: LlmProvider,
    tools: Sequence[ToolDefinition],
    budget: ModelBudget,
    state: ConversationState,
    current_input: str,
    base_system_prompt: str | None,
    summary: ConversationSummary | None,
    preferences: Sequence[UserPreference],
    summary_through: int,
    context_start: int,
) -> tuple[int, str | None]:
    messages = _provider_messages(
        state, current_input, base_system_prompt, summary, preferences,
        summary_through, context_start,
    )
    system = messages[0].content if messages and messages[0].role is ChatRole.SYSTEM else None
    return provider.estimate_request(
        messages, tools, options=GenerationOptions(budget.max_output_tokens),
    ).input_tokens, system


def _summary_estimate(
    provider: LlmProvider,
    budget: ModelBudget,
    previous: ConversationSummary | None,
    messages: Sequence[ChatMessage],
) -> int:
    request = (
        ChatMessage(ChatRole.SYSTEM, SUMMARY_SYSTEM_PROMPT),
        ChatMessage(ChatRole.USER, build_summary_input(previous, messages)),
    )
    return provider.estimate_request(
        request, (), options=GenerationOptions(budget.summary_output_tokens),
    ).input_tokens


def _largest_summary_end(
    provider: LlmProvider,
    budget: ModelBudget,
    state: ConversationState,
    start: int,
    end_limit: int,
) -> int:
    """按完整轮次二分最大连续前缀；估算随历史追加单调不减。"""

    turns = (end_limit - start) // 2
    if turns <= 0:
        return start
    if _summary_estimate(provider, budget, state.summary, state.messages[start:start + 2]) > budget.summary_input_limit:
        return start
    low, high = 1, turns
    while low < high:
        middle = (low + high + 1) // 2
        candidate = start + middle * 2
        if _summary_estimate(provider, budget, state.summary, state.messages[start:candidate]) <= budget.summary_input_limit:
            low = middle
        else:
            high = middle - 1
    return start + low * 2


async def prepare_context(
    state: ConversationState,
    current_input: str,
    base_system_prompt: str | None,
    provider: LlmProvider,
    tools: Sequence[ToolDefinition],
    budget: ModelBudget,
    summarize: SummaryCallback,
    *,
    request_id: str,
    retry_allowed: bool = True,
    on_context_event: ContextEventHandler | None = None,
    now: datetime | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> PreparedContext:
    """至多摘要一次，再按目标/硬上限淘汰原始历史。"""

    preferences = extract_explicit_preferences(current_input, state.preferences, now=now)
    summary = state.summary
    summary_through = state.summary_through_message_count
    context_start = state.context_start_message_count
    before, _ = _estimate_business(
        provider, tools, budget, state, current_input, base_system_prompt,
        summary, preferences, summary_through, context_start,
    )
    # 当前规则、偏好、工具和输入不可裁剪；先检查可避免一次注定无用的摘要调用。
    irreducible, _ = _estimate_business(
        provider, tools, budget, state, current_input, base_system_prompt,
        None, preferences, 0, len(state.messages),
    )
    if irreducible > budget.input_limit:
        raise ValueError("current input exceeds the context window")

    protected_start = max(0, len(state.messages) - budget.recent_turns * 2)
    summary_limit = max(protected_start, context_start)
    should_summarize = (
        retry_allowed
        and ((before >= budget.trigger_tokens and summary_through < summary_limit) or summary_through < context_start)
    )
    attempted = False
    summary_usage = None
    failure_reason = None
    summarized_turns = 0
    outcome = "skipped"

    if should_summarize:
        summary_end = _largest_summary_end(provider, budget, state, summary_through, summary_limit)
        if summary_end == summary_through:
            failure_reason = "batch_too_large"
            outcome = "rejected"
        else:
            attempted = True
            started = clock()
            if on_context_event is not None:
                on_context_event({
                    "type": "context_compaction_started", "request_id": request_id,
                    "before_input_tokens": before,
                })
            try:
                async with asyncio.timeout(budget.summary_timeout_seconds):
                    result = await summarize(
                        summary, state.messages[summary_through:summary_end], budget, request_id,
                    )
                summary_usage = result.token_usage
                if result.finish_reason == "output_limit":
                    failure_reason = "output_limit"
                else:
                    summary = parse_conversation_summary(
                        result.output_text, output_token_limit=budget.summary_output_tokens,
                    )
                    summarized_turns = (summary_end - summary_through) // 2
                    summary_through = summary_end
                    context_start = max(context_start, summary_through)
                    outcome = "summarized"
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                failure_reason = "timeout"
            except LlmProviderError:
                failure_reason = "provider_error"
            except ValueError:
                failure_reason = "invalid_summary"
            if failure_reason is not None:
                outcome = "rejected"
            if on_context_event is not None:
                after_attempt, _ = _estimate_business(
                    provider, tools, budget, state, current_input, base_system_prompt,
                    summary, preferences, summary_through, context_start,
                )
                on_context_event({
                    "type": "context_compaction_finished", "request_id": request_id,
                    "outcome": outcome, "reason": failure_reason,
                    "before_input_tokens": before, "after_input_tokens": after_attempt,
                    "summarized_turns": summarized_turns,
                    "omitted_turns": (context_start - summary_through) // 2,
                    "duration_ms": round((clock() - started) * 1000, 2),
                })

    current, system = _estimate_business(
        provider, tools, budget, state, current_input, base_system_prompt,
        summary, preferences, summary_through, context_start,
    )
    evicted = False
    while context_start < protected_start and current > budget.target_tokens:
        context_start += 2
        evicted = True
        current, system = _estimate_business(
            provider, tools, budget, state, current_input, base_system_prompt,
            summary, preferences, summary_through, context_start,
        )
    while context_start < len(state.messages) and current > budget.input_limit:
        context_start += 2
        evicted = True
        current, system = _estimate_business(
            provider, tools, budget, state, current_input, base_system_prompt,
            summary, preferences, summary_through, context_start,
        )
    if current > budget.input_limit and summary is not None:
        summary = None
        summary_through = 0
        current, system = _estimate_business(
            provider, tools, budget, state, current_input, base_system_prompt,
            summary, preferences, summary_through, context_start,
        )
    if current > budget.input_limit:
        raise ValueError("current input exceeds the context window")
    if evicted and outcome == "skipped":
        outcome = "evicted"
    omitted = (context_start - summary_through) // 2
    return PreparedContext(
        state.messages[context_start:], summary, summary_through, context_start,
        preferences, system, current, summary_usage, attempted, failure_reason,
        summarized_turns, omitted, outcome,
    )
