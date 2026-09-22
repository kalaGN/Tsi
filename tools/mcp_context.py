"""MCP 请求中的模型日志正文脱敏标记。"""

from contextvars import ContextVar


MCP_REDACT_LOGS: ContextVar[bool] = ContextVar("mcp_redact_logs", default=False)
