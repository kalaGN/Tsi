"""Runtime 可选结构化轨迹契约，不依赖具体评测实现。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from app.services.llm.contracts import ChatMessage, TokenUsage


@dataclass(frozen=True, slots=True)
class RequestStartedTraceEvent:
    request_id: str
    provider: str
    model: str
    messages: tuple[ChatMessage, ...]
    event_type: str = "request_started"


@dataclass(frozen=True, slots=True)
class ModelStepCompletedTraceEvent:
    request_id: str
    step_number: int
    duration_ms: float
    upstream_status: int
    output_chars: int
    tool_names: tuple[str, ...]
    visible_tools: tuple[str, ...]
    token_usage: TokenUsage | None
    event_type: str = "model_step_completed"


@dataclass(frozen=True, slots=True)
class ToolCallStartedTraceEvent:
    request_id: str
    step_number: int
    call_id: str
    tool_name: str
    arguments_json: str
    event_type: str = "tool_call_started"


@dataclass(frozen=True, slots=True)
class ToolApprovalCompletedTraceEvent:
    request_id: str
    call_id: str
    tool_name: str
    approved: bool
    duration_ms: float
    event_type: str = "tool_approval_completed"


@dataclass(frozen=True, slots=True)
class ToolCallCompletedTraceEvent:
    request_id: str
    step_number: int
    call_id: str
    tool_name: str
    status: str
    duration_ms: float
    output_text: str
    event_type: str = "tool_call_completed"


@dataclass(frozen=True, slots=True)
class RequestCompletedTraceEvent:
    request_id: str
    output_text: str
    token_usage: TokenUsage | None
    event_type: str = "request_completed"


@dataclass(frozen=True, slots=True)
class RequestFailedTraceEvent:
    request_id: str
    error_code: str
    error_message: str
    event_type: str = "request_failed"


TraceEvent: TypeAlias = (
    RequestStartedTraceEvent
    | ModelStepCompletedTraceEvent
    | ToolCallStartedTraceEvent
    | ToolApprovalCompletedTraceEvent
    | ToolCallCompletedTraceEvent
    | RequestCompletedTraceEvent
    | RequestFailedTraceEvent
)


class TraceObserver(Protocol):
    """同步接收单次请求事件；实现不得阻塞 Runtime。"""

    def record(self, event: TraceEvent) -> None:
        ...


def emit_trace(observer: TraceObserver | None, event: TraceEvent) -> None:
    """只在调用方显式提供 Observer 时发射事件。"""

    if observer is not None:
        observer.record(event)
