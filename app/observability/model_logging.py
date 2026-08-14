"""模型事件的结构化 stderr 与人类可读本地日志配置。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, TextIO
from uuid import uuid4
from zoneinfo import ZoneInfo


LOGGER_NAME = "app.model_calls"
MAX_LOG_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
DEFAULT_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "model-calls.log"

_HANDLER_MARKER = "_model_log_handler_kind"
_CONFIGURATION_LOCK = Lock()
_LOCAL_LOG_TIMEZONE = ZoneInfo("Asia/Shanghai")
_READABLE_SEPARATOR = "=" * 80
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
        "arguments_json",
    ),
    "llm_tool_result": (
        "request_id",
        "call_id",
        "tool_name",
        "status",
        "duration_ms",
        "output_chars",
        "output_text",
    ),
    "llm_tool_approval": (
        "request_id",
        "call_id",
        "tool_name",
        "approved",
        "paths_count",
        "diff_chars",
        "duration_ms",
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


class _ModelEventReadableFormatter(logging.Formatter):
    """把同一白名单事件渲染为适合直接阅读的中文分块日志。"""

    _EVENT_NAMES = {
        "llm_request": "模型请求",
        "llm_response": "模型响应",
        "llm_http_request": "HTTP 请求",
        "llm_http_response": "HTTP 响应",
        "llm_http_error": "HTTP 错误",
        "llm_tool_call": "工具调用",
        "llm_tool_result": "工具结果",
        "llm_tool_approval": "工具审批",
    }
    _ERROR_NAMES = {"timeout": "超时", "connection": "连接失败"}
    _STATUS_NAMES = {"success": "成功", "error": "错误"}

    def format(self, record: logging.LogRecord) -> str:
        event_name = record.event
        timestamp = datetime.fromtimestamp(
            record.created,
            timezone.utc,
        ).astimezone(_LOCAL_LOG_TIMEZONE)
        lines = [
            f"时间：{timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} "
            f"{timestamp.strftime('%z')[:3]}:{timestamp.strftime('%z')[3:]}",
            f"事件：{self._EVENT_NAMES[event_name]}",
        ]
        lines.extend(self._metadata_lines(record, event_name))
        for title, value, parse_json in self._content_sections(record, event_name):
            lines.extend(("", f"【{title}】", _render_log_content(value, parse_json)))
        # 块间保留空行，长请求体和连续工具步骤更容易目视区分。
        lines.extend((_READABLE_SEPARATOR, ""))
        return "\n".join(lines)

    def _metadata_lines(
        self,
        record: logging.LogRecord,
        event_name: str,
    ) -> list[str]:
        """按事件类型输出稳定元数据，避免把 LogRecord 其他字段带入文件。"""

        lines = [f"请求ID：{record.request_id}"]
        if event_name.startswith("llm_http_") or event_name in {
            "llm_request",
            "llm_response",
        }:
            lines.extend((f"Provider：{record.provider}", f"模型：{record.model}"))
        if event_name.startswith("llm_tool_"):
            lines.extend((f"调用ID：{record.call_id}", f"工具：{record.tool_name}"))

        if event_name == "llm_request":
            lines.append(f"输入长度：{record.input_chars} 字符")
        elif event_name == "llm_response":
            lines.append(f"输出长度：{record.output_chars} 字符")
        elif event_name == "llm_http_request":
            lines.extend((f"方法：{record.method}", f"URL：{record.url}"))
        elif event_name == "llm_http_response":
            lines.extend(
                (
                    f"HTTP 状态：{record.status_code}",
                    f"耗时：{record.duration_ms} ms",
                    f"响应类型：{record.response_content_type or '-'}",
                )
            )
        elif event_name == "llm_http_error":
            lines.extend(
                (
                    f"错误类型：{self._ERROR_NAMES.get(record.error_type, record.error_type)}",
                    f"耗时：{record.duration_ms} ms",
                )
            )
        elif event_name == "llm_tool_call":
            lines.append(f"参数长度：{record.arguments_chars} 字符")
        elif event_name == "llm_tool_result":
            lines.extend(
                (
                    f"状态：{self._STATUS_NAMES.get(record.status, record.status)}",
                    f"耗时：{record.duration_ms} ms",
                    f"输出长度：{record.output_chars} 字符",
                )
            )
        elif event_name == "llm_tool_approval":
            lines.extend(
                (
                    f"审批结果：{'通过' if record.approved else '拒绝'}",
                    f"路径数量：{record.paths_count}",
                    f"Diff 长度：{record.diff_chars} 字符",
                    f"耗时：{record.duration_ms} ms",
                )
            )
        return lines

    @staticmethod
    def _content_sections(
        record: logging.LogRecord,
        event_name: str,
    ) -> tuple[tuple[str, object, bool], ...]:
        """返回内容区标题、值以及是否尝试解析字符串 JSON。"""

        if event_name == "llm_request":
            return (("输入内容", record.input_text, False),)
        if event_name == "llm_response":
            return (("输出内容", record.output_text, False),)
        if event_name == "llm_http_request":
            return (
                ("请求 Header", record.headers, False),
                ("超时配置", record.timeout, False),
                ("请求体", record.request_body, False),
            )
        if event_name == "llm_tool_call":
            return (("工具参数", record.arguments_json, True),)
        if event_name == "llm_tool_result":
            return (("工具输出", record.output_text, True),)
        return ()


def _render_log_content(value: object, parse_json: bool) -> str:
    """美化结构化正文；非 JSON 字符串保持原始内容和换行。"""

    rendered_value = value
    if parse_json and isinstance(value, str):
        try:
            rendered_value = json.loads(value)
        except (TypeError, ValueError):
            return value
    if isinstance(rendered_value, (Mapping, list, tuple)):
        try:
            return json.dumps(rendered_value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(value)
    return str(rendered_value)


class _SafeRotatingFileHandler(RotatingFileHandler):
    """将日志文件写入错误隔离在诊断旁路，不影响业务请求。"""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        return None


def configure_model_logging(
    *,
    stream: TextIO | None = None,
    log_path: Path | None = None,
    enable_stream: bool = True,
) -> None:
    """幂等配置本地日志，并按入口需要启用或关闭终端输出。"""

    logger = logging.getLogger(LOGGER_NAME)
    stream_formatter = _ModelEventJsonFormatter()
    file_formatter = _ModelEventReadableFormatter()
    resolved_path = DEFAULT_LOG_PATH if log_path is None else Path(log_path)

    with _CONFIGURATION_LOCK:
        logger.setLevel(logging.INFO)
        logger.propagate = False

        if not enable_stream:
            # Textual 独占终端绘制；移除本模块的 StreamHandler，避免日志覆盖 TUI。
            for handler in list(logger.handlers):
                if getattr(handler, _HANDLER_MARKER, None) != "stream":
                    continue
                logger.removeHandler(handler)
                handler.close()
        elif not _has_handler(logger, "stream"):
            stream_handler = logging.StreamHandler(stream)
            setattr(stream_handler, _HANDLER_MARKER, "stream")
            stream_handler.setFormatter(stream_formatter)
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
        file_handler.setFormatter(file_formatter)
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
    arguments_json: str,
) -> None:
    """记录一次白名单工具调用及模型提供的完整 JSON 参数。"""

    logging.getLogger(LOGGER_NAME).info(
        "llm_tool_call",
        extra={
            "event": "llm_tool_call",
            "request_id": request_id,
            "call_id": call_id,
            "tool_name": tool_name,
            "arguments_chars": arguments_chars,
            "arguments_json": arguments_json,
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
    output_text: str,
) -> None:
    """记录工具结果和耗时；安全错误仍由 Registry 固定正文。"""

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
            "output_text": output_text,
        },
    )


def log_model_tool_approval(
    *,
    request_id: str,
    call_id: str,
    tool_name: str,
    approved: bool,
    paths_count: int,
    diff_chars: int,
    duration_ms: float,
) -> None:
    """记录审批决定的有限元数据，不记录文件路径或 Diff 正文。"""

    logging.getLogger(LOGGER_NAME).info(
        "llm_tool_approval",
        extra={
            "event": "llm_tool_approval",
            "request_id": request_id,
            "call_id": call_id,
            "tool_name": tool_name,
            "approved": approved,
            "paths_count": paths_count,
            "diff_chars": diff_chars,
            "duration_ms": duration_ms,
        },
    )


def _has_handler(logger: logging.Logger, kind: str) -> bool:
    """仅识别本模块创建的 Handler，不干预应用其他日志配置。"""

    return any(
        getattr(handler, _HANDLER_MARKER, None) == kind
        for handler in logger.handlers
    )
