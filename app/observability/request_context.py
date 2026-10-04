"""任务局部的链路关联与有界生命周期事件，不保存请求正文。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import logging
import time
from functools import wraps
from uuid import uuid4


@dataclass(frozen=True)
class Correlation:
    request_id: str
    session_id: str | None = None
    task_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    call_id: str | None = None


_CORRELATION: ContextVar[Correlation | None] = ContextVar("agent_correlation", default=None)
_REQUEST: ContextVar["AgentRequest | None"] = ContextVar("agent_request", default=None)


def correlation_fields() -> dict[str, object]:
    context = _CORRELATION.get()
    if context is None:
        return {}
    return {key: value for key, value in vars(context).items() if value is not None}


def current_request_id() -> str | None:
    context = _CORRELATION.get()
    return context.request_id if context else None


@contextmanager
def request_span(*, call_id: str | None = None, task_id: str | None = None):
    """子调用复用请求 ID，仅为内部阶段创建子 span。"""
    context = _CORRELATION.get()
    if context is None:
        yield
        return
    token = _CORRELATION.set(replace(
        context, span_id=uuid4().hex, parent_span_id=context.span_id,
        call_id=call_id or context.call_id, task_id=task_id or context.task_id,
    ))
    try:
        yield
    finally:
        _CORRELATION.reset(token)


def agent_phase(phase: str) -> None:
    request = _REQUEST.get()
    if request is not None:
        request.phase(phase)


def span_call(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with request_span():
            return await function(*args, **kwargs)
    return wrapped


class AgentRequest:
    """每个入口绑定一次；终态幂等，等待时间与实际执行时间分开统计。"""

    def __init__(self, *, entrypoint: str, request_id: str | None = None,
                 session_id: str | None = None, task_id: str | None = None,
                 provider: str | None = None, model: str | None = None):
        self.request_id = request_id or uuid4().hex
        self.entrypoint, self.provider, self.model = entrypoint, provider, model
        self.context = Correlation(self.request_id, session_id, task_id, uuid4().hex)
        self.started = self.changed = time.monotonic()
        self.current_phase = "received"
        self.wait_ms = 0.0
        self.finished = False
        self.disconnected = False

    def bind(self):
        self.tokens = (_CORRELATION.set(self.context), _REQUEST.set(self))
        self._log("agent_request_started")
        return self

    def close(self):
        _REQUEST.reset(self.tokens[1])
        _CORRELATION.reset(self.tokens[0])

    def _elapsed_phase(self):
        now = time.monotonic()
        elapsed = (now - self.changed) * 1000
        if self.current_phase in {"plan_confirmation", "tool_approval"}:
            self.wait_ms += elapsed
        self.changed = now
        return elapsed

    def phase(self, phase):
        if self.finished or phase == self.current_phase:
            return
        previous = self.current_phase
        elapsed = self._elapsed_phase()
        self.current_phase = phase
        self._log("agent_phase_changed", previous_phase=previous,
                  phase_duration_ms=round(elapsed, 2))

    def finish(self, outcome, error_code=None):
        if self.finished:
            return
        self._elapsed_phase()
        self.finished = True
        total = (time.monotonic() - self.started) * 1000
        self._log("agent_request_finished", outcome=outcome, error_code=error_code,
                  duration_ms=round(total, 2), wait_ms=round(self.wait_ms, 2),
                  execution_ms=round(max(0, total - self.wait_ms), 2))

    def _log(self, event, **fields):
        logging.getLogger("app.model_calls").info(event, extra={
            "event": event, "request_id": self.request_id,
            "entrypoint": self.entrypoint, "provider": self.provider,
            "model": self.model, "phase": self.current_phase,
            **fields,
        })
