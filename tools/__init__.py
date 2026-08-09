"""应用内只读工具的稳定公共入口。"""

from tools.builtin import GetCurrentTimeTool
from tools.contracts import (
    Tool,
    ToolArgumentError,
    ToolCall,
    ToolDefinition,
    ToolPayloadLimitError,
    ToolResult,
)
from tools.registry import ToolRegistry


def create_default_registry() -> ToolRegistry:
    """创建只包含项目已批准只读工具的独立 Registry。"""

    return ToolRegistry((GetCurrentTimeTool(),))


__all__ = [
    "GetCurrentTimeTool",
    "Tool",
    "ToolArgumentError",
    "ToolCall",
    "ToolDefinition",
    "ToolPayloadLimitError",
    "ToolRegistry",
    "ToolResult",
    "create_default_registry",
]
