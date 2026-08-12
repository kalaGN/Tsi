"""Provider 中立的工具定义、审批、调用结果与安全异常。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Protocol, runtime_checkable


class ToolEffect(str, Enum):
    """宿主用于决定是否必须审批的固定副作用等级。"""

    READ_ONLY = "read_only"
    MUTATING = "mutating"


class ToolErrorCode(str, Enum):
    """工具可以主动报告、但不能自定义正文的安全错误分类。"""

    PROTECTED_PATH = "protected_path"
    WORKSPACE_CONFLICT = "workspace_conflict"
    CHECK_TIMEOUT = "check_timeout"
    CHECK_UNAVAILABLE = "check_unavailable"


@dataclass(frozen=True)
class ToolDefinition:
    """提供给模型的工具名称、用途和参数结构。"""

    name: str
    description: str
    parameters: Mapping[str, object]
    effect: ToolEffect = ToolEffect.READ_ONLY
    max_argument_bytes: int = 8 * 1024


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


@dataclass(frozen=True)
class ToolApprovalRequest:
    """写工具在实际执行前交给本地交互层的有界预览。"""

    call_id: str
    tool_name: str
    title: str
    paths: tuple[str, ...]
    diff_text: str
    fingerprint: str


ToolApprovalHandler = Callable[[ToolApprovalRequest], Awaitable[bool]]
ToolResultHandler = Callable[[ToolCall, ToolResult], None]


@dataclass
class ToolExecutionContext:
    """一次用户请求独享的审批回调和拒绝去重状态。"""

    approval_handler: ToolApprovalHandler | None = None
    denied_fingerprints: set[str] = field(default_factory=set)


class Tool(Protocol):
    """根目录工具包支持的最小异步执行契约。"""

    definition: ToolDefinition

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        ...


@runtime_checkable
class ApprovalTool(Protocol):
    """有副作用 Tool 必须提供不落盘的审批预览。"""

    async def preview(
        self,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> ToolApprovalRequest:
        ...


class ToolArgumentError(Exception):
    """工具主动报告的安全参数错误，不携带底层异常。"""


class ToolRejectedError(Exception):
    """工具以固定分类拒绝预期操作，不允许携带外部正文。"""

    def __init__(self, code: ToolErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class ToolPayloadLimitError(Exception):
    """模型参数超过执行边界时终止当前工具循环。"""
