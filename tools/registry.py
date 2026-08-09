"""显式白名单工具注册、参数解析和安全串行执行。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

from tools.contracts import (
    Tool,
    ToolArgumentError,
    ToolCall,
    ToolDefinition,
    ToolPayloadLimitError,
    ToolResult,
)


MAX_ARGUMENT_BYTES = 8 * 1024
MAX_RESULT_BYTES = 32 * 1024
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_ERROR_MESSAGES = {
    "invalid_arguments": "Tool arguments are invalid",
    "unknown_tool": "Tool is not available",
    "execution_failed": "Tool execution failed",
    "result_too_large": "Tool result is too large",
}


class ToolRegistry:
    """冻结已注册工具，并阻止模型绕过白名单执行任意代码。"""

    def __init__(self, tools: Sequence[Tool]) -> None:
        normalized = tuple(tools)
        if not normalized:
            raise ValueError("at least one tool must be registered")

        by_name: dict[str, Tool] = {}
        definitions: list[ToolDefinition] = []
        for tool in normalized:
            definition = tool.definition
            _validate_definition(definition)
            if definition.name in by_name:
                raise ValueError("tool names must be unique")
            by_name[definition.name] = tool
            definitions.append(definition)

        self._tools = by_name
        self._definitions = tuple(definitions)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    async def execute_all(
        self,
        calls: Sequence[ToolCall],
    ) -> tuple[ToolResult, ...]:
        """按模型响应顺序执行；可恢复失败不阻断同批其他调用。"""

        results: list[ToolResult] = []
        for call in calls:
            results.append(await self.execute(call))
        return tuple(results)

    async def execute(self, call: ToolCall) -> ToolResult:
        """解析并执行单次白名单调用，普通异常只返回有限错误。"""

        if len(call.arguments_json.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise ToolPayloadLimitError("Tool arguments exceed the size limit")

        try:
            arguments = json.loads(call.arguments_json)
        except (TypeError, json.JSONDecodeError):
            return _error_result(call.call_id, "invalid_arguments")
        if not isinstance(arguments, dict):
            return _error_result(call.call_id, "invalid_arguments")

        tool = self._tools.get(call.name)
        if tool is None:
            return _error_result(call.call_id, "unknown_tool")

        try:
            value = await tool.invoke(arguments)
            output = _serialize_envelope({"ok": True, "data": value})
        except ToolArgumentError:
            return _error_result(call.call_id, "invalid_arguments")
        except Exception:
            # 工具实现属于不可信执行边界，底层异常不得回传模型。
            return _error_result(call.call_id, "execution_failed")

        if len(output.encode("utf-8")) > MAX_RESULT_BYTES:
            return _error_result(call.call_id, "result_too_large")
        return ToolResult(call_id=call.call_id, output=output)


def _validate_definition(definition: ToolDefinition) -> None:
    if not isinstance(definition, ToolDefinition):
        raise TypeError("tool definition is invalid")
    if not _TOOL_NAME_PATTERN.fullmatch(definition.name):
        raise ValueError("tool name is invalid")
    if (
        not isinstance(definition.description, str)
        or not definition.description.strip()
    ):
        raise ValueError("tool description is invalid")
    if (
        not isinstance(definition.parameters, Mapping)
        or definition.parameters.get("type") != "object"
    ):
        raise ValueError("tool parameters must use an object schema")


def _error_result(call_id: str, code: str) -> ToolResult:
    output = _serialize_envelope(
        {
            "ok": False,
            "error": {"code": code, "message": _SAFE_ERROR_MESSAGES[code]},
        }
    )
    return ToolResult(call_id=call_id, output=output, is_error=True)


def _serialize_envelope(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
