import asyncio

import pytest

from app.evaluation.contracts import ReplayStep
from app.evaluation.replay import ReplayProvider
from app.runtime.context_compaction import SummaryCallResult, prepare_context
from app.runtime.memory import ConversationState, ConversationSummary
from app.runtime.model_budget import ModelBudget
from app.services.llm.contracts import ChatMessage, ChatRole, TokenUsage
from tools import create_default_registry


SUMMARY = ConversationSummary("继续开发", (), (), (), ("完成实现",), (), ())


def turn(number, size=200):
    return (
        ChatMessage(ChatRole.USER, f"问{number}" + "中" * size),
        ChatMessage(ChatRole.ASSISTANT, f"答{number}" + "文" * size),
    )


def provider():
    return ReplayProvider(((ReplayStep("unused"),),))


def policy(**changes):
    return ModelBudget(**{"context_window_tokens": 12800, **changes})


def run_prepare(state, current="继续", summarize=None, budget=None, retry_allowed=True, events=None):
    async def default(previous, batch, active_budget, request_id):
        return SummaryCallResult(SUMMARY.to_json(), TokenUsage(10, 2, 12), "completed")
    return asyncio.run(prepare_context(
        state, current, "规则", provider(), create_default_registry().definitions,
        budget or policy(), summarize or default, request_id="request",
        retry_allowed=retry_allowed,
        on_context_event=(events.append if events is not None else None),
    ))


def test_compaction_summarizes_contiguous_old_turns_and_keeps_recent_turns():
    messages = sum((turn(index, 350) for index in range(10)), ())
    calls, events = [], []

    async def summarize(previous, batch, budget, request_id):
        calls.append((previous, batch, request_id))
        return SummaryCallResult(SUMMARY.to_json(), TokenUsage(10, 2, 12), "completed")

    prepared = run_prepare(
        ConversationState(messages=messages), summarize=summarize,
        budget=policy(recent_turns=3, trigger_percent=20, target_percent=15),
        events=events,
    )
    assert calls and calls[0][1] == messages[:14]
    assert prepared.summary == SUMMARY
    assert prepared.summary_through_message_count == 14
    assert prepared.context_start_message_count >= 14
    assert prepared.context_messages == messages[prepared.context_start_message_count:]
    assert [event["type"] for event in events] == ["context_compaction_started", "context_compaction_finished"]


def test_summary_failure_keeps_s_but_eviction_can_advance_c_and_gap_is_reported():
    messages = sum((turn(index, 500) for index in range(10)), ())

    async def invalid(*args):
        return SummaryCallResult("not json", None, "completed")

    prepared = run_prepare(
        ConversationState(messages=messages), summarize=invalid,
        budget=policy(recent_turns=2, trigger_percent=20, target_percent=10),
    )
    assert prepared.summary is None
    assert prepared.summary_through_message_count == 0
    assert prepared.context_start_message_count > 0
    assert prepared.omitted_turns == prepared.context_start_message_count // 2
    assert prepared.failure_reason == "invalid_summary"


def test_pending_gap_triggers_catchup_below_soft_trigger():
    messages = sum((turn(index, 20) for index in range(5)), ())
    prepared = run_prepare(
        ConversationState(messages, None, 0, 4),
        budget=policy(trigger_percent=99, target_percent=90, recent_turns=5),
    )
    assert prepared.summary == SUMMARY
    assert prepared.summary_through_message_count == 4
    assert prepared.context_start_message_count == 4


def test_recent_turns_can_remain_above_target_when_below_hard_limit():
    messages = sum((turn(index, 300) for index in range(4)), ())
    prepared = run_prepare(
        ConversationState(messages=messages), retry_allowed=False,
        budget=policy(recent_turns=4, trigger_percent=99, target_percent=1),
    )
    assert prepared.context_start_message_count == 0
    assert prepared.input_tokens > policy(target_percent=1).target_tokens
    assert prepared.input_tokens <= policy().input_limit


def test_single_oversized_old_turn_does_not_call_summarizer_or_skip_coverage():
    messages = turn(0, 9000) + turn(1, 10)
    calls = []

    async def summarize(*args):
        calls.append(args)
        return SummaryCallResult(SUMMARY.to_json(), None, "completed")

    prepared = run_prepare(
        ConversationState(messages=messages), summarize=summarize,
        budget=policy(recent_turns=1, trigger_percent=2, target_percent=1),
    )
    assert calls == []
    assert prepared.failure_reason == "batch_too_large"
    assert prepared.summary_through_message_count == 0


def test_summary_output_limit_is_rejected_even_when_json_is_valid():
    async def limited(*args):
        return SummaryCallResult(SUMMARY.to_json(), None, "output_limit")
    prepared = run_prepare(
        ConversationState(messages=sum((turn(i, 500) for i in range(5)), ())),
        summarize=limited, budget=policy(recent_turns=1, trigger_percent=2, target_percent=1),
    )
    assert prepared.summary is None
    assert prepared.failure_reason == "output_limit"


def test_irreducible_current_request_is_rejected_without_summary_call():
    calls = []
    async def summarize(*args):
        calls.append(args)
        return SummaryCallResult(SUMMARY.to_json(), None, "completed")
    with pytest.raises(ValueError, match="context window"):
        run_prepare(ConversationState(), current="中" * 5000, summarize=summarize)
    assert calls == []


def test_user_cancellation_during_summary_propagates():
    started = asyncio.Event()
    async def summarize(*args):
        started.set()
        await asyncio.Event().wait()

    async def scenario():
        messages = sum((turn(i, 500) for i in range(5)), ())
        task = asyncio.create_task(prepare_context(
            ConversationState(messages=messages), "继续", None, provider(), (),
            policy(recent_turns=1, trigger_percent=2, target_percent=1), summarize,
            request_id="cancel",
        ))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
