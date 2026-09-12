"""将 Runtime Trace 收集为单次评测轨迹，并集中脱敏。"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.evaluation.contracts import (
    AgentRunTrace,
    HarnessFingerprint,
    ModelStepTrace,
    ToolCallTrace,
)
from app.runtime.trace import (
    ModelStepCompletedTraceEvent,
    RequestCompletedTraceEvent,
    RequestFailedTraceEvent,
    RequestStartedTraceEvent,
    ToolApprovalCompletedTraceEvent,
    ToolCallCompletedTraceEvent,
    ToolCallStartedTraceEvent,
    TraceEvent,
)
from app.services.llm.contracts import ChatMessage


MAX_TRACE_FIELD_CHARS = 32 * 1024
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|cookie|password|secret|密码|密钥|令牌)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:bearer\s+)[A-Za-z0-9._~+/-]+|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+"
)
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[^\s/]+/)+[^\s,;]*")


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sanitize_value(value: object, *, workspace_root: Path | None = None, key: str | None = None) -> object:
    """递归移除常见密钥、宿主绝对路径和无界正文。"""

    if key is not None and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): sanitize_value(item_value, workspace_root=workspace_root, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item, workspace_root=workspace_root) for item in value]
    if isinstance(value, str):
        text = value
        if workspace_root is not None:
            text = text.replace(str(workspace_root), "[WORKSPACE]")
        text = _SENSITIVE_TEXT.sub("[REDACTED]", text)
        text = _ABSOLUTE_PATH.sub("[ABSOLUTE_PATH]", text)
        if len(text) > MAX_TRACE_FIELD_CHARS:
            return {
                "text": text[:MAX_TRACE_FIELD_CHARS],
                "truncated": True,
                "original_chars": len(text),
            }
        return text
    if value is None or type(value) in {bool, int, float}:
        return value
    return sanitize_value(str(value), workspace_root=workspace_root)


class TraceCollector:
    """收集一个 Trial 的严格事件序列。"""

    def __init__(
        self,
        *,
        run_id: str,
        case_id: str,
        trial: int,
        mode: str,
        provider: str,
        model: str,
        fingerprint: HarnessFingerprint,
        workspace_root: Path,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], str] = utc_now_text,
    ) -> None:
        self._run_id = run_id
        self._case_id = case_id
        self._trial = trial
        self._mode = mode
        self._provider = provider
        self._model = model
        self._fingerprint = fingerprint
        self._workspace_root = workspace_root
        self._clock = clock
        self._now = now
        self._started_tick = clock()
        self._started_at = now()
        self._request_id: str | None = None
        self._messages: tuple[ChatMessage, ...] = ()
        self._steps: list[ModelStepTrace] = []
        self._calls: list[dict[str, object]] = []
        self._output: str | None = None
        self._usage = None
        self._status: str | None = None
        self._error_code: str | None = None
        self._error_message: str | None = None

    def record(self, event: TraceEvent) -> None:
        if isinstance(event, RequestStartedTraceEvent):
            if self._request_id is not None:
                raise RuntimeError("trace contains duplicate request start")
            self._request_id = event.request_id
            self._messages = event.messages
            return
        if self._request_id is None or event.request_id != self._request_id:
            raise RuntimeError("trace event request id is inconsistent")
        if isinstance(event, ModelStepCompletedTraceEvent):
            self._steps.append(
                ModelStepTrace(
                    event.step_number,
                    event.duration_ms,
                    event.upstream_status,
                    event.output_chars,
                    event.tool_names,
                    event.visible_tools,
                    event.token_usage,
                )
            )
        elif isinstance(event, ToolCallStartedTraceEvent):
            try:
                arguments = json.loads(event.arguments_json)
            except (TypeError, json.JSONDecodeError):
                arguments = event.arguments_json
            self._calls.append(
                {
                    "step_number": event.step_number,
                    "call_id": event.call_id,
                    "tool_name": event.tool_name,
                    "arguments": sanitize_value(arguments, workspace_root=self._workspace_root),
                    "approval": None,
                    "status": None,
                    "duration_ms": None,
                    "result": None,
                }
            )
        elif isinstance(event, ToolApprovalCompletedTraceEvent):
            call = self._find_call(event.call_id, event.tool_name)
            call["approval"] = event.approved
        elif isinstance(event, ToolCallCompletedTraceEvent):
            call = self._find_call(event.call_id, event.tool_name)
            call["status"] = event.status
            call["duration_ms"] = event.duration_ms
            try:
                result = json.loads(event.output_text)
            except (TypeError, json.JSONDecodeError):
                result = event.output_text
            call["result"] = sanitize_value(result, workspace_root=self._workspace_root)
        elif isinstance(event, RequestCompletedTraceEvent):
            self._status = "success"
            self._output = event.output_text
            self._usage = event.token_usage
        elif isinstance(event, RequestFailedTraceEvent):
            self._status = "error"
            self._error_code = event.error_code
            self._error_message = event.error_message

    def finalize(
        self,
        *,
        workspace_files: tuple[tuple[str, str], ...] = (),
        fallback_error_code: str | None = None,
        fallback_error_message: str | None = None,
        warnings: tuple[str, ...] = (),
    ) -> AgentRunTrace:
        """构造不可变轨迹；缺少终止事件时标记基础设施失败。"""

        if self._status is None:
            self._status = "error"
            self._error_code = fallback_error_code or "evaluation_infrastructure"
            self._error_message = fallback_error_message or "Evaluation trace is incomplete"
        safe_calls = tuple(ToolCallTrace(**call) for call in self._calls)
        usage = self._usage
        if usage is None and self._steps and all(step.token_usage is not None for step in self._steps):
            from app.services.llm.contracts import TokenUsage

            usage = TokenUsage(0, 0, 0)
            for step in self._steps:
                usage += step.token_usage
        return AgentRunTrace(
            self._run_id,
            self._case_id,
            self._trial,
            self._mode,
            self._provider,
            self._model,
            self._request_id,
            self._started_at,
            self._now(),
            round((self._clock() - self._started_tick) * 1000, 2),
            self._status,
            self._output,
            self._error_code,
            self._error_message,
            self._messages,
            tuple(self._steps),
            safe_calls,
            usage,
            self._fingerprint,
            workspace_files,
            warnings,
        )

    def force_failure(self, code: str, message: str) -> None:
        """在脚本完整性等评测基础设施失败时覆盖业务终态。"""

        self._status = "error"
        self._error_code = code
        self._error_message = message
        self._output = None

    def _find_call(self, call_id: str, tool_name: str) -> dict[str, object]:
        for call in reversed(self._calls):
            if call["call_id"] == call_id and call["tool_name"] == tool_name:
                return call
        raise RuntimeError("tool trace completion has no matching start")
