"""Provider 共用的纯输入估算与有序工具结果校验。"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.services.llm.contracts import (
    ProviderContextLimitError,
    ProviderInvalidRequestError,
    RequestBudgetEstimate,
)
from tools.contracts import ToolCall, ToolResult


def estimate_text_tokens(text: str) -> int:
    """保留轻量估算法；JSON 协议开销和非 ASCII 内容均计入。"""

    ascii_count = sum(character.isascii() for character in text)
    return (ascii_count + 3) // 4 + len(text) - ascii_count


def estimate_payload(payload: Mapping[str, Any]) -> RequestBudgetEstimate:
    projection = {
        key: payload[key]
        for key in ("messages", "input", "tools", "tool_choice")
        if key in payload
    }
    serialized = json.dumps(projection, ensure_ascii=False, separators=(",", ":"))
    return RequestBudgetEstimate(estimate_text_tokens(serialized))


def default_request_guard(estimate: RequestBudgetEstimate, *, max_output_tokens: int = 4096) -> None:
    """直接使用适配器的调用方也有硬边界；Runtime 注入模型专属守卫。"""

    if estimate.input_tokens > 128_000 - max_output_tokens - 4096:
        raise ProviderContextLimitError()


def validate_pending_results(
    calls: Sequence[ToolCall], results: Sequence[ToolResult], *, complete: bool,
) -> None:
    """部分预览只接受调用序列的前缀，发送则要求每个调用都有结果。"""

    if len(results) > len(calls) or (complete and len(results) != len(calls)):
        raise ProviderInvalidRequestError()
    if any(
        not isinstance(result, ToolResult) or call.call_id != result.call_id
        for call, result in zip(calls, results)
    ):
        raise ProviderInvalidRequestError()
