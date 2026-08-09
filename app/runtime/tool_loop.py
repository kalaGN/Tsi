"""Provider 中立的有界模型步骤与只读工具执行循环。"""

import time
from collections.abc import Callable

from app.observability.model_logging import (
    log_model_tool_call,
    log_model_tool_result,
)
from app.services.llm.contracts import (
    LlmTurn,
    ModelStep,
    ProviderInvalidResponseError,
)
from tools.contracts import ToolPayloadLimitError, ToolResult
from tools.registry import ToolRegistry


MAX_MODEL_STEPS = 5
MAX_TOOL_CALLS_PER_STEP = 4


class ToolLoopLimitError(Exception):
    """工具请求超过固定成本或载荷边界。"""


async def run_tool_loop(
    turn: LlmTurn,
    registry: ToolRegistry,
    *,
    request_id: str,
    clock: Callable[[], float] = time.monotonic,
) -> ModelStep:
    """串行执行模型要求的白名单工具，直到得到最终文本或触达上限。"""

    results: tuple[ToolResult, ...] = ()
    for step_number in range(1, MAX_MODEL_STEPS + 1):
        step = await turn.next(results)
        if not step.tool_calls:
            if not isinstance(step.output_text, str) or not step.output_text:
                raise ProviderInvalidResponseError(
                    "Upstream service returned an invalid response"
                )
            return step

        # 最后一步的调用无法再被模型消费，因此不执行这些无用工具。
        if (
            step_number == MAX_MODEL_STEPS
            or len(step.tool_calls) > MAX_TOOL_CALLS_PER_STEP
        ):
            raise ToolLoopLimitError("Tool call limit exceeded")

        current_results: list[ToolResult] = []
        for call in step.tool_calls:
            log_model_tool_call(
                request_id=request_id,
                call_id=call.call_id,
                tool_name=call.name,
                arguments_chars=len(call.arguments_json),
            )
            started_at = clock()
            try:
                result = await registry.execute(call)
            except ToolPayloadLimitError as exc:
                raise ToolLoopLimitError("Tool call limit exceeded") from exc
            duration_ms = round((clock() - started_at) * 1000, 2)
            log_model_tool_result(
                request_id=request_id,
                call_id=call.call_id,
                tool_name=call.name,
                status="error" if result.is_error else "success",
                duration_ms=duration_ms,
                output_chars=len(result.output),
            )
            current_results.append(result)
        results = tuple(current_results)

    # 循环范围已覆盖所有分支，仅作为类型和未来修改的防御性兜底。
    raise ToolLoopLimitError("Tool call limit exceeded")
