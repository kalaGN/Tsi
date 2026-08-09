"""Provider 中立的工具定义、调用结果与安全异常。"""

from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True)
class ToolDefinition:
    """提供给模型的工具名称、用途和参数结构。"""

    name: str
    description: str
    parameters: Mapping[str, object]


@dataclass(frozen=True)
class ToolCall:
    """从模型协议转换得到的一次工具调用。"""

    call_id: str
    name: str
    arguments_json: str


@dataclass(frozen=True)
class ToolResult:
    """以文本形式回传模型的关联工具结果。"""

    call_id: str
    output: str
    is_error: bool = False


class Tool(Protocol):
    """根目录工具包支持的最小异步执行契约。"""

    definition: ToolDefinition

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        ...


class ToolArgumentError(Exception):
    """工具主动报告的安全参数错误，不携带底层异常。"""


class ToolPayloadLimitError(Exception):
    """模型参数超过执行边界时终止当前工具循环。"""
