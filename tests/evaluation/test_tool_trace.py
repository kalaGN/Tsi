import asyncio

from app.evaluation.contracts import ReplayStep
from app.evaluation.replay import ReplayProvider
from app.runtime.chat import run_chat
from app.runtime.trace import ModelStepCompletedTraceEvent, ToolCallCompletedTraceEvent, ToolCallStartedTraceEvent
from app.services.llm.contracts import TokenUsage
from tools import create_default_registry
from tools.contracts import ToolCall


class Recorder:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


def test_tool_loop_trace_captures_visible_tools_calls_and_usage():
    recorder = Recorder()
    provider = ReplayProvider(
        ((
            ReplayStep(tool_calls=(ToolCall("time", "get_current_time", '{"timezone":"UTC"}'),), token_usage=TokenUsage(4, 1, 5)),
            ReplayStep("完成", token_usage=TokenUsage(6, 2, 8)),
        ),)
    )

    result = asyncio.run(
        run_chat(
            "现在几点",
            provider=provider,
            registry=create_default_registry(),
            trace_observer=recorder,
        )
    )

    steps = [event for event in recorder.events if isinstance(event, ModelStepCompletedTraceEvent)]
    assert len(steps) == 2
    assert steps[0].visible_tools == ("get_current_time",)
    assert steps[0].token_usage == TokenUsage(4, 1, 5)
    assert any(isinstance(event, ToolCallStartedTraceEvent) for event in recorder.events)
    assert any(isinstance(event, ToolCallCompletedTraceEvent) and event.status == "success" for event in recorder.events)
    assert result.token_usage == TokenUsage(10, 3, 13)
