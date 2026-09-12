import asyncio

import pytest

from app.evaluation.contracts import ReplayStep
from app.evaluation.replay import ReplayProvider
from app.runtime.chat import ChatRuntimeError, run_chat
from app.runtime.trace import RequestCompletedTraceEvent, RequestFailedTraceEvent, RequestStartedTraceEvent


class Recorder:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


def test_chat_runtime_emits_request_lifecycle():
    recorder = Recorder()
    provider = ReplayProvider(((ReplayStep("完成"),),))

    result = asyncio.run(run_chat("开始", provider=provider, trace_observer=recorder))

    assert result.output_text == "完成"
    assert isinstance(recorder.events[0], RequestStartedTraceEvent)
    assert isinstance(recorder.events[-1], RequestCompletedTraceEvent)
    assert recorder.events[0].messages[-1].content == "开始"


def test_chat_runtime_emits_tool_limit_failure():
    from app.runtime.tool_loop import ToolLoopLimits
    from tools.contracts import ToolCall

    recorder = Recorder()
    provider = ReplayProvider(((ReplayStep(tool_calls=(ToolCall("1", "get_current_time", "{}"),)),),))

    with pytest.raises(ChatRuntimeError):
        asyncio.run(
            run_chat(
                "开始",
                provider=provider,
                trace_observer=recorder,
                tool_loop_limits=ToolLoopLimits(1, 1, 1),
            )
        )

    assert isinstance(recorder.events[-1], RequestFailedTraceEvent)
    assert recorder.events[-1].error_code == "tool_limit"
