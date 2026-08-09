import asyncio
import json

import pytest

from app.runtime import tool_loop
from app.runtime.tool_loop import ToolLoopLimitError, run_tool_loop
from app.services.llm.contracts import ModelStep
from tools.contracts import ToolCall, ToolDefinition
from tools.registry import MAX_ARGUMENT_BYTES, ToolRegistry


class RecordingTool:
    definition = ToolDefinition(
        "lookup",
        "Look up a test value",
        {"type": "object", "properties": {}},
    )

    def __init__(self, events=None, error=None):
        self.events = [] if events is None else events
        self.error = error

    async def invoke(self, arguments):
        self.events.append(dict(arguments))
        if self.error is not None:
            raise self.error
        return {"received": dict(arguments)}


class FakeTurn:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.received_results = []

    async def next(self, tool_results=()):
        self.received_results.append(tuple(tool_results))
        return next(self.steps)


def tool_step(*calls):
    return ModelStep(200, None, tuple(calls))


def final_step(text="done"):
    return ModelStep(200, text, ())


def test_tool_loop_returns_direct_model_text_without_executing_tool():
    tool = RecordingTool()
    turn = FakeTurn([final_step("direct")])

    result = asyncio.run(
        run_tool_loop(turn, ToolRegistry([tool]), request_id="a" * 32)
    )

    assert result.output_text == "direct"
    assert turn.received_results == [()]
    assert tool.events == []


def test_tool_loop_executes_tool_and_returns_result_to_next_model_step():
    tool = RecordingTool()
    turn = FakeTurn(
        [
            tool_step(ToolCall("call-1", "lookup", '{"value":"中文"}')),
            final_step(),
        ]
    )

    result = asyncio.run(
        run_tool_loop(turn, ToolRegistry([tool]), request_id="b" * 32)
    )

    assert result.output_text == "done"
    assert tool.events == [{"value": "中文"}]
    returned = turn.received_results[1][0]
    assert returned.call_id == "call-1"
    assert json.loads(returned.output) == {
        "ok": True,
        "data": {"received": {"value": "中文"}},
    }


def test_tool_loop_executes_multiple_calls_serially_and_logs_metadata(monkeypatch):
    events = []
    calls = []
    results = []
    tool = RecordingTool(events)
    turn = FakeTurn(
        [
            tool_step(
                ToolCall("call-1", "lookup", '{"order":1}'),
                ToolCall("call-2", "lookup", '{"order":2}'),
            ),
            final_step(),
        ]
    )
    monkeypatch.setattr(
        tool_loop,
        "log_model_tool_call",
        lambda **fields: calls.append(fields),
    )
    monkeypatch.setattr(
        tool_loop,
        "log_model_tool_result",
        lambda **fields: results.append(fields),
    )
    ticks = iter([1.0, 1.01, 2.0, 2.025])

    asyncio.run(
        run_tool_loop(
            turn,
            ToolRegistry([tool]),
            request_id="c" * 32,
            clock=lambda: next(ticks),
        )
    )

    assert events == [{"order": 1}, {"order": 2}]
    assert [event["call_id"] for event in calls] == ["call-1", "call-2"]
    assert [event["request_id"] for event in calls] == ["c" * 32, "c" * 32]
    assert [event["duration_ms"] for event in results] == [10.0, 25.0]
    assert [event["status"] for event in results] == ["success", "success"]


def test_tool_loop_returns_recoverable_tool_error_to_model():
    tool = RecordingTool(error=RuntimeError("sensitive detail"))
    turn = FakeTurn(
        [tool_step(ToolCall("call-1", "lookup", "{}")), final_step("recovered")]
    )

    result = asyncio.run(
        run_tool_loop(turn, ToolRegistry([tool]), request_id="d" * 32)
    )

    returned = turn.received_results[1][0]
    assert returned.is_error is True
    assert json.loads(returned.output)["error"]["code"] == "execution_failed"
    assert "sensitive" not in returned.output
    assert result.output_text == "recovered"


def test_tool_loop_stops_before_executing_calls_from_fifth_model_step():
    tool = RecordingTool()
    call = ToolCall("call", "lookup", "{}")
    turn = FakeTurn([tool_step(call) for _ in range(5)])

    with pytest.raises(ToolLoopLimitError):
        asyncio.run(
            run_tool_loop(turn, ToolRegistry([tool]), request_id="e" * 32)
        )

    assert len(turn.received_results) == 5
    assert len(tool.events) == 4


def test_tool_loop_rejects_more_than_four_calls_without_executing_any():
    tool = RecordingTool()
    turn = FakeTurn(
        [
            tool_step(
                *(ToolCall(f"call-{index}", "lookup", "{}") for index in range(5))
            )
        ]
    )

    with pytest.raises(ToolLoopLimitError):
        asyncio.run(
            run_tool_loop(turn, ToolRegistry([tool]), request_id="f" * 32)
        )

    assert tool.events == []


def test_tool_loop_maps_oversized_arguments_to_terminal_limit():
    tool = RecordingTool()
    arguments = "你" * (MAX_ARGUMENT_BYTES // 3 + 1)
    turn = FakeTurn([tool_step(ToolCall("call-1", "lookup", arguments))])

    with pytest.raises(ToolLoopLimitError):
        asyncio.run(
            run_tool_loop(turn, ToolRegistry([tool]), request_id="1" * 32)
        )

    assert tool.events == []


def test_tool_loop_does_not_swallow_tool_cancellation():
    tool = RecordingTool(error=asyncio.CancelledError())
    turn = FakeTurn([tool_step(ToolCall("call-1", "lookup", "{}"))])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_tool_loop(turn, ToolRegistry([tool]), request_id="2" * 32)
        )
