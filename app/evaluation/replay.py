"""不访问网络的确定性 LLM Provider。"""

from __future__ import annotations

from collections.abc import Sequence

from app.evaluation.contracts import EvaluationConfigError, ReplayStep
from app.services.llm.contracts import (
    ChatMessage,
    GenerationOptions,
    LlmTurn,
    ModelStep,
    RequestGuard,
    RequestBudgetEstimate,
    TextDeltaHandler,
)
from app.services.llm.budget import default_request_guard, estimate_payload, validate_pending_results
from tools.contracts import ToolDefinition, ToolResult


class ReplayRequestState:
    """回放协议的可预算输入状态，不调用真实 Provider 或外部服务。"""

    def __init__(self, messages=(), tools=()):
        self.messages = [{"role": message.role.value, "content": message.content} for message in messages]
        self.tools = tuple(tools)
        self.pending_calls = ()

    def _candidate(self, results, *, complete):
        validate_pending_results(self.pending_calls, results, complete=complete)
        return self.messages + [
            {"role": "tool", "tool_call_id": result.call_id, "content": result.output}
            for result in results
        ]

    def estimate(self, results=(), *, complete=True) -> RequestBudgetEstimate:
        payload = {"messages": self._candidate(results, complete=complete)}
        if self.tools:
            payload["tools"] = [{
                "type": "function", "function": {
                    "name": tool.name, "description": tool.description,
                    "parameters": dict(tool.parameters),
                },
            } for tool in self.tools]
            payload["tool_choice"] = "auto"
        return estimate_payload(payload)

    def prepare(self, results):
        self.messages = self._candidate(results, complete=True)
        self.pending_calls = ()

    def accept(self, step):
        if step.tool_calls:
            self.messages.append({
                "role": "assistant", "content": step.output_text,
                "tool_calls": [{
                    "id": call.call_id, "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments_json},
                } for call in step.tool_calls],
            })
            self.pending_calls = step.tool_calls


class ReplayTurn:
    """按声明顺序消费单组步骤，并记录动态工具替换。"""

    def __init__(self, steps: tuple[ReplayStep, ...], *, messages=(), tools=(), options=GenerationOptions(), request_guard=default_request_guard) -> None:
        self._steps = steps
        self._index = 0
        self.tool_replacements: list[tuple[ToolDefinition, ...]] = []
        self.received_results: list[tuple[ToolResult, ...]] = []
        self._state = ReplayRequestState(messages, tools)
        self._options = options
        self._request_guard = request_guard

    def estimate_pending(self, tool_results=(), *, complete=True) -> RequestBudgetEstimate:
        return self._state.estimate(tuple(tool_results), complete=complete)

    async def next(
        self,
        tool_results: Sequence[ToolResult] = (),
        *,
        on_text_delta: TextDeltaHandler | None = None,
    ) -> ModelStep:
        if self._index >= len(self._steps):
            raise EvaluationConfigError("replay turn has no remaining step")
        self._request_guard(self.estimate_pending(tool_results))
        self._state.prepare(tool_results)
        self.received_results.append(tuple(tool_results))
        step = self._steps[self._index]
        self._index += 1
        if on_text_delta is not None and step.output_text is not None:
            on_text_delta(step.output_text)
        result = ModelStep(
            step.upstream_status,
            step.output_text,
            step.tool_calls,
            step.token_usage,
            "tool_calls" if step.tool_calls else "completed",
        )
        self._state.accept(result)
        return result

    def replace_tools(self, tools: tuple[ToolDefinition, ...]) -> None:
        self.tool_replacements.append(tuple(tools))
        self._state.tools = tuple(tools)

    async def aclose(self) -> None:
        """回放 Turn 不持有外部资源，保留统一关闭接口。"""

    @property
    def exhausted(self) -> bool:
        return self._index == len(self._steps)


class ReplayProvider:
    """为每个 Runtime Turn 分配一组回放步骤。"""

    name = "replay"
    model = "scripted-v1"

    def __init__(self, turns: tuple[tuple[ReplayStep, ...], ...]) -> None:
        if not turns or any(not turn for turn in turns):
            raise EvaluationConfigError("replay provider requires nonempty turns")
        self._scripts = turns
        self._index = 0
        self.created_messages: list[tuple[ChatMessage, ...]] = []
        self.created_tools: list[tuple[ToolDefinition, ...]] = []
        self.turns: list[ReplayTurn] = []

    @property
    def api_key_configured(self) -> bool:
        return False

    def create_turn(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition],
        *,
        request_id: str,
        options: GenerationOptions = GenerationOptions(),
        request_guard: RequestGuard = default_request_guard,
    ) -> LlmTurn:
        if self.turns and not self.turns[-1].exhausted:
            raise EvaluationConfigError("previous replay turn is not exhausted")
        if self._index >= len(self._scripts):
            raise EvaluationConfigError("replay provider has no remaining turn")
        turn = ReplayTurn(self._scripts[self._index], messages=messages, tools=tools, options=options, request_guard=request_guard)
        self._index += 1
        self.created_messages.append(tuple(messages))
        self.created_tools.append(tuple(tools))
        self.turns.append(turn)
        return turn

    def estimate_request(self, messages, tools, *, options=GenerationOptions()):
        return ReplayRequestState(messages, tools).estimate()

    def assert_consumed(self) -> None:
        """拒绝把未执行脚本误报为完整评测。"""

        if self._index != len(self._scripts) or any(not turn.exhausted for turn in self.turns):
            raise EvaluationConfigError("replay script was not fully consumed")
