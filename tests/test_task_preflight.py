"""任务预判必须是一次无工具、失败关闭的模型调用。"""

import asyncio
import json

import pytest

from app.runtime.task_preflight import TaskPreflightError, assess_task
from app.services.llm.contracts import ModelStep
from tools.contracts import ToolCall


class FakeTurn:
    def __init__(self, step, *, delay=0):
        self.step = step
        self.delay = delay
        self.closed = False
        self.calls = 0

    async def next(self, tool_results=(), *, on_text_delta=None):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return self.step

    async def aclose(self):
        self.closed = True


class FakeProvider:
    name = "deepseek"
    model = "test-model"

    def __init__(self, step, *, delay=0):
        self.turn = FakeTurn(step, delay=delay)
        self.messages = None
        self.tools = None
        self.options = None

    def create_turn(self, messages, tools, *, request_id, options, request_guard):
        self.messages = tuple(messages)
        self.tools = tuple(tools)
        self.options = options
        assert request_id
        assert callable(request_guard)
        return self.turn


def step(payload, *, tool_calls=(), finish_reason="completed"):
    return ModelStep(200, json.dumps(payload, ensure_ascii=False), tuple(tool_calls), finish_reason=finish_reason)


def test_preflight_direct_uses_one_tool_free_model_step():
    provider = FakeProvider(step({"kind": "direct", "reason": "简单问答", "steps": [], "question": None}))
    decision = asyncio.run(assess_task(provider, "你好", context="上轮讨论 Python"))
    assert decision.kind == "direct"
    assert decision.steps == ()
    assert provider.tools == ()
    assert provider.turn.calls == 1
    assert provider.turn.closed
    assert provider.options.max_output_tokens <= 700
    assert provider.messages[0].role.value == "system"
    assert provider.messages[1].role.value == "user"


def test_preflight_planned_requires_checkable_steps():
    provider = FakeProvider(step({
        "kind": "planned", "reason": "涉及多处代码",
        "steps": [
            {"action": "检查当前接口", "deliverable": "列出受影响路由"},
            {"action": "实现并测试", "deliverable": "相关测试通过"},
        ],
        "question": None,
    }))
    decision = asyncio.run(assess_task(provider, "实现新接口并测试"))
    assert decision.kind == "planned"
    assert len(decision.steps) == 2
    assert decision.steps[0].deliverable == "列出受影响路由"


@pytest.mark.parametrize("payload", [
    {"kind": "planned", "reason": "复杂", "steps": [], "question": None},
    {"kind": "direct", "reason": "简单", "steps": [{"action": "执行", "deliverable": "结果"}], "question": None},
    {"kind": "clarify", "reason": "缺信息", "steps": [], "question": None},
    {"kind": "planned", "reason": "复杂", "steps": [{"action": "执行", "deliverable": "结果"}] * 7, "question": None},
    {"kind": "direct", "reason": "简单", "steps": [], "question": None, "extra": "wrong"},
])
def test_preflight_rejects_invalid_contract(payload):
    provider = FakeProvider(step(payload))
    with pytest.raises(TaskPreflightError, match="预判结果无效"):
        asyncio.run(assess_task(provider, "任务"))
    assert provider.turn.closed


def test_preflight_rejects_tool_call_without_executing_it():
    provider = FakeProvider(step(
        {"kind": "direct", "reason": "简单", "steps": [], "question": None},
        tool_calls=(ToolCall("call-1", "delete_workspace_file", "{}"),),
    ))
    with pytest.raises(TaskPreflightError, match="预判结果无效"):
        asyncio.run(assess_task(provider, "删除文件"))
    assert provider.tools == ()
    assert provider.turn.closed


def test_preflight_timeout_fails_closed_and_releases_turn():
    provider = FakeProvider(step({"kind": "direct", "reason": "简单", "steps": [], "question": None}), delay=0.05)
    with pytest.raises(TaskPreflightError, match="预判超时"):
        asyncio.run(assess_task(provider, "你好", timeout_seconds=0.001))
    assert provider.turn.closed


def test_preflight_rejects_oversized_input_before_creating_turn():
    provider = FakeProvider(step({"kind": "direct", "reason": "简单", "steps": [], "question": None}))
    with pytest.raises(TaskPreflightError, match="输入过长"):
        asyncio.run(assess_task(provider, "中" * 6000))
    assert provider.messages is None
