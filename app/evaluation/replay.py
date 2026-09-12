"""不访问网络的确定性 LLM Provider。"""

from __future__ import annotations

from collections.abc import Sequence

from app.evaluation.contracts import EvaluationConfigError, ReplayStep
from app.services.llm.contracts import (
    ChatMessage,
    LlmTurn,
    ModelStep,
    TextDeltaHandler,
)
from tools.contracts import ToolDefinition, ToolResult


class ReplayTurn:
    """按声明顺序消费单组步骤，并记录动态工具替换。"""

    def __init__(self, steps: tuple[ReplayStep, ...]) -> None:
        self._steps = steps
        self._index = 0
        self.tool_replacements: list[tuple[ToolDefinition, ...]] = []
        self.received_results: list[tuple[ToolResult, ...]] = []

    async def next(
        self,
        tool_results: Sequence[ToolResult] = (),
        *,
        on_text_delta: TextDeltaHandler | None = None,
    ) -> ModelStep:
        if self._index >= len(self._steps):
            raise EvaluationConfigError("replay turn has no remaining step")
        self.received_results.append(tuple(tool_results))
        step = self._steps[self._index]
        self._index += 1
        if on_text_delta is not None and step.output_text is not None:
            on_text_delta(step.output_text)
        return ModelStep(
            step.upstream_status,
            step.output_text,
            step.tool_calls,
            step.token_usage,
        )

    def replace_tools(self, tools: tuple[ToolDefinition, ...]) -> None:
        self.tool_replacements.append(tuple(tools))

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
    ) -> LlmTurn:
        if self.turns and not self.turns[-1].exhausted:
            raise EvaluationConfigError("previous replay turn is not exhausted")
        if self._index >= len(self._scripts):
            raise EvaluationConfigError("replay provider has no remaining turn")
        turn = ReplayTurn(self._scripts[self._index])
        self._index += 1
        self.created_messages.append(tuple(messages))
        self.created_tools.append(tuple(tools))
        self.turns.append(turn)
        return turn

    def assert_consumed(self) -> None:
        """拒绝把未执行脚本误报为完整评测。"""

        if self._index != len(self._scripts) or any(not turn.exhausted for turn in self.turns):
            raise EvaluationConfigError("replay script was not fully consumed")
