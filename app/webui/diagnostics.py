"""本机页面诊断：固定分类、有限字段和独立轮转，不接收自由文本。"""

from collections import deque
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import time
from zoneinfo import ZoneInfo

from local_paths import log_root


EVENTS = {"script_error", "unhandled_rejection", "request_failed", "stream_parse_error",
          "stream_disconnected", "terminal_state_stuck", "terminal_received", "terminal_applied"}
ERROR_TYPES = {"Error", "TypeError", "SyntaxError", "RangeError", "ReferenceError",
               "AbortError", "NetworkError", "Unknown", "ProtocolError"}
STREAM_EVENTS = {"request_started", "task_state", "task_verification", "preflight_started",
                 "preflight_result", "plan_approval_required", "execution_started",
                 "preflight_clarify", "preflight_cancelled", "text_delta", "text_reset",
                 "tool_approval_required", "tool_finished", "context_compaction_started",
                 "context_compaction_finished", "context_updated", "context_settings_warning",
                 "mcp_warning", "completed", "failed", "cancelled"}
_HANDLERS = {}


def validate_diagnostic(payload):
    """拒绝未知字段而非原样保留，阻止正文、密钥和任意堆栈进入日志。"""
    fields = {"event", "request_id", "session_id", "task_id", "last_event", "error_type",
              "page_version", "elapsed_ms", "line", "column", "script"}
    if not isinstance(payload, dict) or set(payload) - fields or payload.get("event") not in EVENTS:
        raise ValueError("invalid diagnostic")
    result = {"event": payload["event"]}
    for name in ("request_id", "session_id", "task_id"):
        value = payload.get(name)
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-f0-9-]{1,64}", value)):
            raise ValueError("invalid correlation")
        result[name] = value
    for name, allowed in (("last_event", STREAM_EVENTS), ("error_type", ERROR_TYPES),
                          ("script", {"app.js", "diagnostics.js", "context-settings.js", "model-config.js", "service-config.js"}),
                          ("page_version", {"1"})):
        value = payload.get(name)
        if value is not None and (not isinstance(value, str) or value not in allowed):
            raise ValueError("invalid classification")
        result[name] = value
    for name in ("elapsed_ms", "line", "column"):
        value = payload.get(name)
        if value is not None and (type(value) not in {int, float} or not 0 <= value <= 86400000):
            raise ValueError("invalid numeric value")
        result[name] = value
    return result


class DiagnosticLimiter:
    """整个本机页面共用预算，避免通过更换会话标识绕过限流。"""

    def __init__(self, limit=60, clock=time.monotonic):
        self.limit, self.clock, self.hits = limit, clock, deque()

    def allow(self):
        now = self.clock()
        while self.hits and self.hits[0] <= now - 60:
            self.hits.popleft()
        if len(self.hits) >= self.limit:
            return False
        self.hits.append(now)
        return True


def write_diagnostic(payload):
    """诊断写入失败不影响聊天；测试文件与正式文件严格分离。"""
    category = "tests" if os.environ.get("PYTEST_CURRENT_TEST") else "runtime"
    date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    path = log_root() / category / f"{date}-web-client.log"
    try:
        handler = _HANDLERS.get(path)
        if handler is None:
            # 长期运行跨日时关闭旧 Handler，避免文件描述符随天数增长。
            for previous in _HANDLERS.values():
                previous.close()
            _HANDLERS.clear()
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
            _HANDLERS[path] = handler
        data = {"timestamp": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="milliseconds"), **payload}
        handler.emit(logging.LogRecord("app.web_client", logging.INFO, "", 0,
                                      json.dumps(data, ensure_ascii=False), (), None))
    except (OSError, ValueError):
        pass
