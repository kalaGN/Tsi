import asyncio
import json

import pytest

from app.runtime import tool_loop
from app.runtime.tool_loop import (
    DEFAULT_TOOL_LOOP_LIMITS,
    WORKSPACE_TOOL_LOOP_LIMITS,
    ToolLoopLimitError,
    run_tool_loop,
)
from app.services.llm.contracts import ModelStep, TokenUsage
from tools.contracts import (
    SKILL_INSTALL_APPROVAL_WARNING_TEXT,
    SkillInstallApprovalRequest,
    ToolApprovalRequest,
    ToolCall,
    ToolDefinition,
    ToolEffect,
)
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

    async def next(self, tool_results=(), *, on_text_delta=None):
        self.received_results.append(tuple(tool_results))
        return next(self.steps)


def tool_step(*calls):
    return ModelStep(200, None, tuple(calls))


def final_step(text="done"):
    return ModelStep(200, text, ())


class ApprovalRecordingTool(RecordingTool):
    definition = ToolDefinition(
        "write_test",
        "Write a test value",
        {"type": "object", "properties": {}},
        effect=ToolEffect.MUTATING,
    )

    async def preview(self, call_id, arguments):
        return ToolApprovalRequest(
            call_id=call_id,
            tool_name=self.definition.name,
            title="Apply one change",
            paths=("demo.txt",),
            diff_text="--- a/demo.txt\n+++ b/demo.txt\n@@ -0,0 +1 @@\n+中文\n",
            fingerprint="a" * 64,
        )


class InstallApprovalRecordingTool(RecordingTool):
    definition = ToolDefinition(
        "install_test",
        "Install a test value",
        {"type": "object", "properties": {}},
        effect=ToolEffect.MUTATING,
    )

    async def preview(self, call_id, arguments):
        return SkillInstallApprovalRequest(
            call_id=call_id,
            tool_name=self.definition.name,
            title="安装 Skill",
            source_type="codex_home",
            source_display="~/.codex/skills/demo",
            target_path=".agents/skills/demo",
            network_access=False,
            warning_text=SKILL_INSTALL_APPROVAL_WARNING_TEXT,
            fingerprint="b" * 64,
        )


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


def test_tool_loop_logs_and_aggregates_usage_from_every_model_step(monkeypatch):
    tool = RecordingTool()
    call = ToolCall("call-1", "lookup", "{}")
    turn = FakeTurn(
        [
            ModelStep(200, None, (call,), TokenUsage(10, 2, 12)),
            ModelStep(200, "done", (), TokenUsage(20, 3, 23)),
        ]
    )
    logged = []
    monkeypatch.setattr(
        tool_loop,
        "log_model_token_usage",
        lambda **fields: logged.append(fields),
    )

    result = asyncio.run(
        run_tool_loop(turn, ToolRegistry([tool]), request_id="8" * 32)
    )

    assert result.output_text == "done"
    assert result.token_usage == TokenUsage(30, 5, 35)
    assert [event["step_number"] for event in logged] == [1, 2]
    assert [event["total_tokens"] for event in logged] == [12, 23]


def test_tool_loop_marks_aggregate_unavailable_when_any_step_lacks_usage(
    monkeypatch,
):
    tool = RecordingTool()
    call = ToolCall("call-1", "lookup", "{}")
    turn = FakeTurn(
        [
            ModelStep(200, None, (call,), TokenUsage(10, 2, 12)),
            ModelStep(200, "done", ()),
        ]
    )
    logged = []
    monkeypatch.setattr(
        tool_loop,
        "log_model_token_usage",
        lambda **fields: logged.append(fields),
    )

    result = asyncio.run(
        run_tool_loop(turn, ToolRegistry([tool]), request_id="9" * 32)
    )

    assert result.token_usage is None
    assert len(logged) == 1
    assert logged[0]["step_number"] == 1


def test_tool_loop_resets_streamed_intermediate_text_before_tools():
    class StreamingTurn(FakeTurn):
        async def next(self, tool_results=(), *, on_text_delta=None):
            step = await super().next(
                tool_results,
                on_text_delta=on_text_delta,
            )
            if on_text_delta is not None and step.output_text:
                on_text_delta(step.output_text)
            return step

    tool = RecordingTool()
    turn = StreamingTurn(
        [
            ModelStep(
                200,
                "checking",
                (ToolCall("call-1", "lookup", "{}"),),
            ),
            final_step("done"),
        ]
    )
    deltas = []
    resets = []

    result = asyncio.run(
        run_tool_loop(
            turn,
            ToolRegistry([tool]),
            request_id="9" * 32,
            on_text_delta=deltas.append,
            on_text_reset=lambda: resets.append("reset"),
        )
    )

    assert deltas == ["checking", "done"]
    assert resets == ["reset"]
    assert result.output_text == "done"


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
    assert [event["arguments_json"] for event in calls] == [
        '{"order":1}',
        '{"order":2}',
    ]
    assert [event["duration_ms"] for event in results] == [10.0, 25.0]
    assert [event["status"] for event in results] == ["success", "success"]
    assert all(json.loads(event["output_text"])["ok"] for event in results)


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


def test_tool_loop_limit_profiles_keep_http_default_and_expand_workspace():
    assert DEFAULT_TOOL_LOOP_LIMITS.max_model_steps == 5
    assert DEFAULT_TOOL_LOOP_LIMITS.max_tool_calls_per_step == 4
    assert DEFAULT_TOOL_LOOP_LIMITS.max_total_tool_calls == 16
    assert WORKSPACE_TOOL_LOOP_LIMITS.max_model_steps == 41
    assert WORKSPACE_TOOL_LOOP_LIMITS.max_tool_calls_per_step == 4
    assert WORKSPACE_TOOL_LOOP_LIMITS.max_total_tool_calls == 40


def test_workspace_loop_allows_forty_sequential_calls_then_final_text():
    tool = RecordingTool()
    steps = [
        tool_step(ToolCall(f"call-{index}", "lookup", "{}"))
        for index in range(40)
    ]
    turn = FakeTurn([*steps, final_step("completed")])

    result = asyncio.run(
        run_tool_loop(
            turn,
            ToolRegistry([tool]),
            request_id="4" * 32,
            limits=WORKSPACE_TOOL_LOOP_LIMITS,
        )
    )

    assert result.output_text == "completed"
    assert len(tool.events) == 40


def test_tool_loop_stops_before_exceeding_total_call_budget():
    tool = RecordingTool()
    calls = [
        tool_step(
            *(ToolCall(f"call-{step}-{index}", "lookup", "{}") for index in range(4))
        )
        for step in range(5)
    ]
    turn = FakeTurn(calls)

    with pytest.raises(ToolLoopLimitError):
        asyncio.run(
            run_tool_loop(
                turn,
                ToolRegistry([tool]),
                request_id="3" * 32,
                limits=DEFAULT_TOOL_LOOP_LIMITS,
            )
        )

    assert len(tool.events) == 16


def test_tool_loop_passes_approval_and_logs_metadata_without_diff(monkeypatch):
    tool = ApprovalRecordingTool()
    turn = FakeTurn(
        [tool_step(ToolCall("call-write", "write_test", "{}")), final_step()]
    )
    approvals = []
    logged = []

    async def approve(request):
        approvals.append(request)
        return True

    monkeypatch.setattr(
        tool_loop,
        "log_model_tool_approval",
        lambda **fields: logged.append(fields),
    )
    ticks = iter([1.0, 1.01, 2.0, 2.025])

    result = asyncio.run(
        run_tool_loop(
            turn,
            ToolRegistry([tool]),
            request_id="4" * 32,
            on_tool_approval=approve,
            clock=lambda: next(ticks),
        )
    )

    assert result.output_text == "done"
    assert len(approvals) == 1
    assert tool.events == [{}]
    assert logged == [
        {
            "request_id": "4" * 32,
            "call_id": "call-write",
            "tool_name": "write_test",
            "approved": True,
            "paths_count": 1,
            "diff_chars": len(approvals[0].diff_text),
            "duration_ms": 990.0,
        }
    ]
    assert "中文" not in json.dumps(logged, ensure_ascii=False)


def test_tool_loop_reports_each_completed_tool_result_to_host():
    tool = RecordingTool()
    call = ToolCall("call-observe", "lookup", '{"value":"中文"}')
    turn = FakeTurn([tool_step(call), final_step()])
    observed = []

    asyncio.run(
        run_tool_loop(
            turn,
            ToolRegistry([tool]),
            request_id="5" * 32,
            on_tool_result=lambda completed_call, result: observed.append(
                (completed_call, result)
            ),
        )
    )

    assert observed[0][0] == call
    assert json.loads(observed[0][1].output)["data"]["received"] == {
        "value": "中文"
    }


def test_tool_loop_logs_non_file_approval_without_assuming_diff_fields(monkeypatch):
    tool = InstallApprovalRecordingTool()
    turn = FakeTurn(
        [tool_step(ToolCall("call-install", "install_test", "{}")), final_step()]
    )
    logged = []

    monkeypatch.setattr(
        tool_loop,
        "log_model_tool_approval",
        lambda **fields: logged.append(fields),
    )

    async def approve(_request):
        return True

    asyncio.run(
        run_tool_loop(
            turn,
            ToolRegistry([tool]),
            request_id="6" * 32,
            on_tool_approval=approve,
        )
    )

    assert logged[0]["paths_count"] == 0
    assert logged[0]["diff_chars"] == 0
