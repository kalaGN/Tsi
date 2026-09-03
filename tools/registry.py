"""显式白名单工具注册、参数解析和安全串行执行。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from tools.contracts import (
    ApprovalTool,
    SCRIPT_APPROVAL_WARNING_TEXT,
    ScriptApprovalRequest,
    Tool,
    ToolApprovalRequest,
    ToolArgumentError,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolErrorCode,
    ToolExecutionContext,
    ToolPayloadLimitError,
    ToolRejectedError,
    ToolResult,
)


MAX_ARGUMENT_BYTES = 8 * 1024
MAX_TOOL_ARGUMENT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 32 * 1024
MAX_TOOL_RESULT_BYTES = 256 * 1024
MAX_APPROVAL_DIFF_BYTES = 64 * 1024
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ERROR_MESSAGES = {
    "invalid_arguments": "Tool arguments are invalid",
    "unknown_tool": "Tool is not available",
    "execution_failed": "Tool execution failed",
    "result_too_large": "Tool result is too large",
    "approval_unavailable": "Tool approval is unavailable",
    "approval_denied": "Tool execution was not approved",
    "protected_path": "Workspace path is protected",
    "workspace_conflict": "Workspace content changed",
    "check_timeout": "Project check timed out",
    "check_unavailable": "Project check is unavailable",
    "script_timeout": "Skill script timed out",
    "script_output_too_large": "Skill script output is too large",
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
            if definition.effect in {
                ToolEffect.MUTATING,
                ToolEffect.EXECUTING,
            } and not isinstance(tool, ApprovalTool):
                raise ValueError(
                    "tools with side effects must provide approval preview"
                )
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

    async def execute(
        self,
        call: ToolCall,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        """解析并执行单次白名单调用，普通异常只返回有限错误。"""

        argument_bytes = len(call.arguments_json.encode("utf-8"))
        if argument_bytes > MAX_TOOL_ARGUMENT_BYTES:
            raise ToolPayloadLimitError("Tool arguments exceed the size limit")

        tool = self._tools.get(call.name)
        if tool is None:
            return _error_result(call.call_id, "unknown_tool")
        if argument_bytes > tool.definition.max_argument_bytes:
            raise ToolPayloadLimitError("Tool arguments exceed the size limit")

        try:
            arguments = json.loads(call.arguments_json)
        except (TypeError, json.JSONDecodeError):
            return _error_result(call.call_id, "invalid_arguments")
        if not isinstance(arguments, dict):
            return _error_result(call.call_id, "invalid_arguments")

        if tool.definition.effect in {
            ToolEffect.MUTATING,
            ToolEffect.EXECUTING,
        }:
            approval_error = await self._request_approval(
                call,
                tool,
                arguments,
                context,
            )
            if approval_error is not None:
                return approval_error

        try:
            value = await tool.invoke(arguments)
            output = _serialize_envelope({"ok": True, "data": value})
        except ToolArgumentError:
            return _error_result(call.call_id, "invalid_arguments")
        except ToolRejectedError as exc:
            return _error_result(call.call_id, exc.code.value)
        except Exception:
            # 工具实现属于不可信执行边界，底层异常不得回传模型。
            return _error_result(call.call_id, "execution_failed")

        if len(output.encode("utf-8")) > tool.definition.max_result_bytes:
            return _error_result(call.call_id, "result_too_large")
        return ToolResult(call_id=call.call_id, output=output)

    async def _request_approval(
        self,
        call: ToolCall,
        tool: Tool,
        arguments: Mapping[str, object],
        context: ToolExecutionContext | None,
    ) -> ToolResult | None:
        """只允许合法预览在本次请求中获得一次显式本地决定。"""

        if not isinstance(tool, ApprovalTool):
            return _error_result(call.call_id, "execution_failed")
        try:
            request = await tool.preview(call.call_id, arguments)
        except ToolArgumentError:
            return _error_result(call.call_id, "invalid_arguments")
        except ToolRejectedError as exc:
            return _error_result(call.call_id, exc.code.value)
        except Exception:
            return _error_result(call.call_id, "execution_failed")

        if not _valid_approval_request(request, call):
            return _error_result(call.call_id, "execution_failed")
        if context is None or context.approval_handler is None:
            return _error_result(call.call_id, "approval_unavailable")
        if (
            isinstance(request, ToolApprovalRequest)
            and request.fingerprint in context.denied_fingerprints
        ):
            return _error_result(call.call_id, "approval_denied")

        try:
            approved = await context.approval_handler(request)
        except Exception:
            return _error_result(call.call_id, "approval_unavailable")
        if approved is not True:
            if approved is False and isinstance(request, ToolApprovalRequest):
                context.denied_fingerprints.add(request.fingerprint)
            if approved is False:
                return _error_result(call.call_id, "approval_denied")
            return _error_result(call.call_id, "approval_unavailable")
        return None


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
    if not isinstance(definition.effect, ToolEffect):
        raise ValueError("tool effect is invalid")
    if (
        not isinstance(definition.max_argument_bytes, int)
        or isinstance(definition.max_argument_bytes, bool)
        or not 1 <= definition.max_argument_bytes <= MAX_TOOL_ARGUMENT_BYTES
    ):
        raise ValueError("tool argument limit is invalid")
    if (
        not isinstance(definition.max_result_bytes, int)
        or isinstance(definition.max_result_bytes, bool)
        or not 1 <= definition.max_result_bytes <= MAX_TOOL_RESULT_BYTES
    ):
        raise ValueError("tool result limit is invalid")


def _valid_approval_request(
    request: object,
    call: ToolCall,
) -> bool:
    """防止写 Tool 伪造无界、错配或越界形状的审批对象。"""

    if isinstance(request, ScriptApprovalRequest):
        return _valid_script_approval_request(request, call)
    if not isinstance(request, ToolApprovalRequest):
        return False
    if request.call_id != call.call_id or request.tool_name != call.name:
        return False
    if (
        not isinstance(request.title, str)
        or not request.title.strip()
        or not isinstance(request.paths, tuple)
        or not request.paths
    ):
        return False
    if any(
        not isinstance(path, str)
        or not path
        or PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        for path in request.paths
    ):
        return False
    return (
        isinstance(request.diff_text, str)
        and bool(request.diff_text)
        and len(request.diff_text.encode("utf-8")) <= MAX_APPROVAL_DIFF_BYTES
        and isinstance(request.fingerprint, str)
        and bool(_FINGERPRINT_PATTERN.fullmatch(request.fingerprint))
    )


def _valid_script_approval_request(
    request: ScriptApprovalRequest,
    call: ToolCall,
) -> bool:
    """只接受有界且不含绝对路径的 Skill 脚本审批预览。"""

    script_path = PurePosixPath(request.script_path)
    return (
        request.call_id == call.call_id
        and request.tool_name == call.name
        and isinstance(request.title, str)
        and bool(request.title.strip())
        and len(request.title.encode("utf-8")) <= 1024
        and isinstance(request.skill_name, str)
        and bool(_SKILL_NAME_PATTERN.fullmatch(request.skill_name))
        and isinstance(request.script_path, str)
        and bool(request.script_path)
        and len(request.script_path) <= 1024
        and not script_path.is_absolute()
        and ".." not in script_path.parts
        and script_path.parts[:1] == ("scripts",)
        and isinstance(request.command_text, str)
        and bool(request.command_text)
        and len(request.command_text.encode("utf-8")) <= MAX_APPROVAL_DIFF_BYTES
        and isinstance(request.warning_text, str)
        and request.warning_text == SCRIPT_APPROVAL_WARNING_TEXT
        and isinstance(request.fingerprint, str)
        and bool(_FINGERPRINT_PATTERN.fullmatch(request.fingerprint))
    )


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
