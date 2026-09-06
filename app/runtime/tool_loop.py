"""Provider 中立的有界模型步骤与只读工具执行循环。"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from app.observability.model_logging import (
    log_model_token_usage,
    log_model_tool_call,
    log_model_tool_approval,
    log_model_tool_result,
)
from app.services.llm.contracts import (
    LlmTurn,
    ModelStep,
    ProviderInvalidResponseError,
    TextDeltaHandler,
    TextResetHandler,
    TokenUsage,
)
from tools.contracts import (
    ToolApprovalRequest,
    ToolApprovalHandler,
    ToolExecutionContext,
    ToolPayloadLimitError,
    ToolResult,
    ToolResultHandler,
)
from tools.registry import ToolRegistry


@dataclass(frozen=True)
class ToolLoopLimits:
    """限制单次模型请求的步骤数与工具调用成本。"""

    max_model_steps: int
    max_tool_calls_per_step: int
    max_total_tool_calls: int


@dataclass(frozen=True)
class ToolLoopResult:
    """工具循环完成后的最终文本和全部模型步骤计量。"""

    output_text: str
    token_usage: TokenUsage | None


DEFAULT_TOOL_LOOP_LIMITS = ToolLoopLimits(5, 4, 16)
# 部分 Provider 每个模型步骤只返回一个工具调用；预留最后一步生成最终回答，
# 才能在不放宽 40 次工具调用硬上限的前提下完整使用调用预算。
WORKSPACE_TOOL_LOOP_LIMITS = ToolLoopLimits(41, 4, 40)


class ToolLoopLimitError(Exception):
    """工具请求超过固定成本或载荷边界。"""


async def run_tool_loop(
    turn: LlmTurn,
    registry: ToolRegistry,
    *,
    request_id: str,
    on_text_delta: TextDeltaHandler | None = None,
    on_text_reset: TextResetHandler | None = None,
    on_tool_approval: ToolApprovalHandler | None = None,
    on_tool_result: ToolResultHandler | None = None,
    limits: ToolLoopLimits = DEFAULT_TOOL_LOOP_LIMITS,
    clock: Callable[[], float] = time.monotonic,
) -> ToolLoopResult:
    """串行执行模型要求的白名单工具，直到得到最终文本或触达上限。"""

    if (
        limits.max_model_steps < 1
        or limits.max_tool_calls_per_step < 1
        or limits.max_total_tool_calls < 0
    ):
        raise ValueError("tool loop limits are invalid")

    async def approve(request):
        """为审批交互增加不含路径和 Diff 正文的审计事件。"""

        if on_tool_approval is None:
            return False
        started_at = clock()
        approved = await on_tool_approval(request)
        duration_ms = round((clock() - started_at) * 1000, 2)
        paths_count = len(request.paths) if isinstance(request, ToolApprovalRequest) else 0
        diff_chars = len(request.diff_text) if isinstance(request, ToolApprovalRequest) else 0
        log_model_tool_approval(
            request_id=request_id,
            call_id=request.call_id,
            tool_name=request.tool_name,
            approved=approved is True,
            paths_count=paths_count,
            diff_chars=diff_chars,
            duration_ms=duration_ms,
        )
        return approved

    execution_context = ToolExecutionContext(
        approval_handler=approve if on_tool_approval is not None else None
    )
    results: tuple[ToolResult, ...] = ()
    executed_calls = 0
    aggregate_usage = TokenUsage(0, 0, 0)
    usage_complete = True
    for step_number in range(1, limits.max_model_steps + 1):
        step = await turn.next(results, on_text_delta=on_text_delta)
        if step.token_usage is None:
            usage_complete = False
        else:
            log_model_token_usage(
                request_id=request_id,
                step_number=step_number,
                input_tokens=step.token_usage.input_tokens,
                output_tokens=step.token_usage.output_tokens,
                total_tokens=step.token_usage.total_tokens,
            )
            aggregate_usage += step.token_usage
        if not step.tool_calls:
            if not isinstance(step.output_text, str) or not step.output_text:
                raise ProviderInvalidResponseError(
                    "Upstream service returned an invalid response"
                )
            return ToolLoopResult(
                step.output_text,
                aggregate_usage if usage_complete else None,
            )

        # 当前模型步骤属于工具中间态，撤销可能已展示的临时文本。
        if on_text_reset is not None:
            on_text_reset()

        # 最后一步的调用无法再被模型消费，因此不执行这些无用工具。
        if (
            step_number == limits.max_model_steps
            or len(step.tool_calls) > limits.max_tool_calls_per_step
            or executed_calls + len(step.tool_calls)
            > limits.max_total_tool_calls
        ):
            raise ToolLoopLimitError("Tool call limit exceeded")

        current_results: list[ToolResult] = []
        for call in step.tool_calls:
            log_model_tool_call(
                request_id=request_id,
                call_id=call.call_id,
                tool_name=call.name,
                arguments_chars=len(call.arguments_json),
                arguments_json=call.arguments_json,
            )
            started_at = clock()
            try:
                result = await registry.execute(call, execution_context)
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
                output_text=result.output,
            )
            if on_tool_result is not None:
                on_tool_result(call, result)
            current_results.append(result)
            executed_calls += 1
        results = tuple(current_results)

    # 循环范围已覆盖所有分支，仅作为类型和未来修改的防御性兜底。
    raise ToolLoopLimitError("Tool call limit exceeded")
