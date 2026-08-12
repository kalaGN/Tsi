"""应用内默认只读工具的稳定公共入口。"""

from tools.builtin import GetCurrentTimeTool
from tools.contracts import (
    ToolApprovalHandler,
    ToolApprovalRequest,
    Tool,
    ToolArgumentError,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolPayloadLimitError,
    ToolResult,
    ToolResultHandler,
)
from tools.registry import ToolRegistry


def create_default_registry() -> ToolRegistry:
    """创建只包含项目默认只读工具的独立 Registry。"""

    return ToolRegistry((GetCurrentTimeTool(),))


__all__ = [
    "GetCurrentTimeTool",
    "Tool",
    "ToolApprovalHandler",
    "ToolApprovalRequest",
    "ToolArgumentError",
    "ToolCall",
    "ToolDefinition",
    "ToolEffect",
    "ToolExecutionContext",
    "ToolPayloadLimitError",
    "ToolRegistry",
    "ToolResult",
    "ToolResultHandler",
    "create_default_registry",
]
