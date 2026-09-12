import asyncio

import pytest

from app.evaluation.contracts import EvaluationConfigError, ReplayStep
from app.evaluation.replay import ReplayProvider
from app.services.llm.contracts import ChatMessage, ChatRole, TokenUsage


def test_replay_provider_records_request_and_streams_text():
    provider = ReplayProvider(((ReplayStep("完成", token_usage=TokenUsage(2, 1, 3)),),))
    messages = (ChatMessage(ChatRole.USER, "开始"),)
    turn = provider.create_turn(messages, (), request_id="r")
    deltas = []

    step = asyncio.run(turn.next(on_text_delta=deltas.append))
    provider.assert_consumed()

    assert step.output_text == "完成"
    assert deltas == ["完成"]
    assert provider.created_messages == [messages]


def test_replay_provider_rejects_missing_or_unconsumed_steps():
    provider = ReplayProvider(((ReplayStep("一"), ReplayStep("二")),))
    turn = provider.create_turn((ChatMessage(ChatRole.USER, "x"),), (), request_id="r")
    asyncio.run(turn.next())

    with pytest.raises(EvaluationConfigError, match="not fully consumed"):
        provider.assert_consumed()

    asyncio.run(turn.next())
    with pytest.raises(EvaluationConfigError, match="no remaining step"):
        asyncio.run(turn.next())


def test_replay_provider_requires_previous_turn_to_finish():
    provider = ReplayProvider(
        (
            (ReplayStep("一"), ReplayStep("二")),
            (ReplayStep("三"),),
        )
    )
    provider.create_turn(
        (ChatMessage(ChatRole.USER, "x"),),
        (),
        request_id="r1",
    )

    with pytest.raises(EvaluationConfigError, match="previous replay turn"):
        provider.create_turn(
            (ChatMessage(ChatRole.USER, "y"),),
            (),
            request_id="r2",
        )
