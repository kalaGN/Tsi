"""模型输入输出的明文 JSON 日志与本地转储配置。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, TextIO
from uuid import uuid4


LOGGER_NAME = "app.model_calls"
MAX_LOG_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
DEFAULT_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "model-calls.log"

_HANDLER_MARKER = "_model_log_handler_kind"
_CONFIGURATION_LOCK = Lock()
# 日志层用固定脱敏值重建请求 Header，从数据流上阻止真实 API Key 进入 Logger。
_REDACTED_REQUEST_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": "Bearer [REDACTED]",
}
_EVENT_FIELDS = {
    "llm_request": (
        "request_id",
        "provider",
        "model",
        "input_chars",
        "input_text",
    ),
    "llm_response": (
        "request_id",
        "provider",
        "model",
        "output_chars",
        "output_text",
    ),
    "llm_http_request": (
        "request_id",
        "provider",
        "model",
        "method",
        "url",
        "headers",
        "request_body",
        "timeout",
    ),
    "llm_http_response": (
        "request_id",
        "provider",
        "model",
        "status_code",
        "duration_ms",
        "response_content_type",
    ),
    "llm_http_error": (
        "request_id",
        "provider",
        "model",
        "error_type",
        "duration_ms",
    ),
    "llm_tool_call": (
        "request_id",
        "call_id",
        "tool_name",
        "arguments_chars",
    ),
    "llm_tool_result": (
        "request_id",
        "call_id",
        "tool_name",
        "status",
        "duration_ms",
        "output_chars",
    ),
}


class _ModelEventJsonFormatter(logging.Formatter):
    """按事件白名单格式化明文内容，不展开其他内部对象。"""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, timezone.utc)
        event_name = record.event
        event = {
            "timestamp": timestamp.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "level": record.levelname,
            "event": event_name,
        }
        # 每类事件仅展开它的固定字段，避免意外序列化 LogRecord。
        event.update(
            (field_name, getattr(record, field_name))
            for field_name in _EVENT_FIELDS[event_name]
        )
        return json.dumps(event, ensure_ascii=False, separators=(",", ":"))


class _SafeRotatingFileHandler(RotatingFileHandler):
    """将日志文件写入错误隔离在诊断旁路，不影响业务请求。"""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        return None


def configure_model_logging(
    *,
    stream: TextIO | None = None,
    log_path: Path | None = None,
) -> None:
    """幂等配置 stderr 和本地转储日志，文件不可用时自动降级。"""

    logger = logging.getLogger(LOGGER_NAME)
    formatter = _ModelEventJsonFormatter()
    resolved_path = DEFAULT_LOG_PATH if log_path is None else Path(log_path)

    with _CONFIGURATION_LOCK:
        logger.setLevel(logging.INFO)
        logger.propagate = False

        if not _has_handler(logger, "stream"):
            stream_handler = logging.StreamHandler(stream)
            setattr(stream_handler, _HANDLER_MARKER, "stream")
            stream_handler.setFormatter(formatter)
            logger.addHandler(stream_handler)

        if _has_handler(logger, "file"):
            return

        try:
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = _SafeRotatingFileHandler(
                resolved_path,
                maxBytes=MAX_LOG_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
                delay=True,
            )
        except OSError:
            # 可观测性不应成为启动或模型调用的单点故障。
            return

        setattr(file_handler, _HANDLER_MARKER, "file")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)


def new_request_id() -> str:
    """为本地日志条目生成不受客户端控制的标识。"""

    return uuid4().hex


def log_model_request(
    *,
    request_id: str,
    provider: str,
    model: str,
    input_chars: int,
    input_text: str,
) -> None:
    """记录模型请求及用户明确要求持久化的完整输入。"""

    logging.getLogger(LOGGER_NAME).info(
        "llm_request",
        extra={
            "event": "llm_request",
            "request_id": request_id,
            "provider": provider,
            "model": model,
            "input_chars": input_chars,
            "input_text": input_text,
        },
    )


def log_model_response(
    *,
    request_id: str,
    provider: str,
    model: str,
    output_chars: int,
    output_text: str,
) -> None:
    """记录与请求关联的完整统一模型输出。"""

    logging.getLogger(LOGGER_NAME).info(
        "llm_response",
        extra={
            "event": "llm_response",
            "request_id": request_id,
            "provider": provider,
            "model": model,
            "output_chars": output_chars,
            "output_text": output_text,
        },
    )


def log_model_http_request(
    *,
    request_id: str,
    provider: str,
    model: str,
    method: str,
    url: str,
    request_body: Mapping[str, Any],
    timeout: Mapping[str, float],
) -> None:
    """记录外部 HTTP 请求边界；Header 用固定脱敏值重建，不接收真实 Authorization。"""

    logging.getLogger(LOGGER_NAME).info(
        "llm_http_request",
        extra={
            "event": "llm_http_request",
            "request_id": request_id,
            "provider": provider,
            "model": model,
            "method": method,
            "url": url,
            "headers": dict(_REDACTED_REQUEST_HEADERS),
            "request_body": request_body,
            "timeout": timeout,
        },
    )


def log_model_http_response(
    *,
    request_id: str,
    provider: str,
    model: str,
    status_code: int,
    duration_ms: float,
    response_content_type: str | None,
) -> None:
    """记录外部 HTTP 响应状态、Content-Type 和耗时，不记录原始响应体。"""

    logging.getLogger(LOGGER_NAME).info(
        "llm_http_response",
        extra={
            "event": "llm_http_response",
            "request_id": request_id,
            "provider": provider,
            "model": model,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "response_content_type": response_content_type,
        },
    )


def log_model_http_error(
    *,
    request_id: str,
    provider: str,
    model: str,
    error_type: str,
    duration_ms: float,
) -> None:
    """记录超时或连接失败的有限分类和耗时，不记录异常原文或堆栈。"""

    logging.getLogger(LOGGER_NAME).info(
        "llm_http_error",
        extra={
            "event": "llm_http_error",
            "request_id": request_id,
            "provider": provider,
            "model": model,
            "error_type": error_type,
            "duration_ms": duration_ms,
        },
    )


def log_model_tool_call(
    *,
    request_id: str,
    call_id: str,
    tool_name: str,
    arguments_chars: int,
) -> None:
    """记录一次白名单工具调用的关联元数据，不重复记录完整参数。"""

    logging.getLogger(LOGGER_NAME).info(
        "llm_tool_call",
        extra={
            "event": "llm_tool_call",
            "request_id": request_id,
            "call_id": call_id,
            "tool_name": tool_name,
            "arguments_chars": arguments_chars,
        },
    )


def log_model_tool_result(
    *,
    request_id: str,
    call_id: str,
    tool_name: str,
    status: str,
    duration_ms: float,
    output_chars: int,
) -> None:
    """记录工具结果状态和耗时，不记录结果正文或异常详情。"""

    logging.getLogger(LOGGER_NAME).info(
        "llm_tool_result",
        extra={
            "event": "llm_tool_result",
            "request_id": request_id,
            "call_id": call_id,
            "tool_name": tool_name,
            "status": status,
            "duration_ms": duration_ms,
            "output_chars": output_chars,
        },
    )


def _has_handler(logger: logging.Logger, kind: str) -> bool:
    """仅识别本模块创建的 Handler，不干预应用其他日志配置。"""

    return any(
        getattr(handler, _HANDLER_MARKER, None) == kind
        for handler in logger.handlers
    )
