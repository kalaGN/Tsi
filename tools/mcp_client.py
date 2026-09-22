"""请求级 MCP 客户端：只暴露显式配置的服务器工具。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from tools.contracts import (
    MCP_APPROVAL_WARNING_TEXT,
    McpApprovalRequest,
    ToolArgumentError,
    ToolDefinition,
    ToolEffect,
    ToolRejectedError,
    ToolErrorCode,
    ToolRuntime,
)
from tools.mcp_context import MCP_REDACT_LOGS
from tools.registry import MAX_ARGUMENT_BYTES, MAX_RESULT_BYTES, ToolRegistry


DEFAULT_MCP_CONFIG = Path(__file__).resolve().parents[1] / "data" / "mcp-servers.json"
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,19}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
MAX_SERVERS = 8
MAX_TOOLS = 20
MAX_TOTAL_SCHEMA_BYTES = 64 * 1024


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()


def load_mcp_config(path: Path = DEFAULT_MCP_CONFIG) -> tuple[McpServerConfig, ...]:
    """读取本机非版本化配置，拒绝不明确的命令和凭据字段。"""

    if path.is_symlink():
        raise ValueError("MCP configuration is invalid")
    if not path.exists():
        return ()
    if not path.is_file() or path.stat().st_size > 32 * 1024:
        raise ValueError("MCP configuration is invalid")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("MCP configuration is invalid") from exc
    if not isinstance(document, dict) or set(document) != {"servers"}:
        raise ValueError("MCP configuration is invalid")
    entries = document["servers"]
    if not isinstance(entries, list) or len(entries) > MAX_SERVERS:
        raise ValueError("MCP configuration is invalid")
    configs: list[McpServerConfig] = []
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get("enabled", True)) is not bool:
            raise ValueError("MCP server configuration is invalid")
        name = entry.get("name")
        transport = entry.get("transport")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in {c.name for c in configs}:
            raise ValueError("MCP server name is invalid or repeated")
        if transport == "stdio":
            if set(entry) - {"name", "transport", "enabled", "command", "args", "env"}:
                raise ValueError("MCP server configuration is invalid")
            command, args = entry.get("command"), entry.get("args", [])
            if (
                not isinstance(command, str) or not command or "\x00" in command
                or not isinstance(args, list) or len(args) > 32
                or any(not isinstance(arg, str) or len(arg) > 2048 or "\x00" in arg for arg in args)
            ):
                raise ValueError("MCP stdio command is invalid")
            config = McpServerConfig(
                name, transport, command=command, args=tuple(args),
                env=_env_refs(entry.get("env", {})),
            )
        elif transport == "streamable_http":
            if set(entry) - {"name", "transport", "enabled", "url", "headers"}:
                raise ValueError("MCP server configuration is invalid")
            url = entry.get("url")
            if not isinstance(url, str) or len(url) > 2048:
                raise ValueError("MCP URL is invalid")
            try:
                parts = urlsplit(url)
                host = parts.hostname
            except ValueError as exc:
                raise ValueError("MCP URL is invalid") from exc
            if (
                parts.scheme not in {"https", "http"} or not host
                or parts.username or parts.password or parts.fragment
                or (parts.scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"})
            ):
                raise ValueError("MCP URL is invalid")
            headers = entry.get("headers", {})
            if (
                not isinstance(headers, dict) or len(headers) > 16
                or any(
                    not isinstance(key, str) or not _HEADER_NAME.fullmatch(key)
                    or key.lower() in {"host", "content-length"}
                    for key in headers
                )
            ):
                raise ValueError("MCP headers are invalid")
            config = McpServerConfig(name, transport, url=url, headers=_env_refs(headers))
        else:
            raise ValueError("MCP transport is invalid")
        if entry.get("enabled", True):
            configs.append(config)
    return tuple(configs)


def _env_refs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or len(value) > 16 or any(
        not isinstance(key, str) or not _ENV_NAME.fullmatch(key)
        or not isinstance(ref, str) or not _ENV_NAME.fullmatch(ref)
        for key, ref in value.items()
    ):
        raise ValueError("MCP environment references are invalid")
    return tuple(value.items())


class McpTool:
    def __init__(self, server: McpServerConfig, name: str, definition: ToolDefinition, client: Client) -> None:
        self.server = server
        self.name = name
        self.definition = definition
        self._client = client

    async def preview(self, call_id: str, arguments: Mapping[str, object]) -> McpApprovalRequest:
        try:
            preview = json.dumps(arguments, ensure_ascii=False, allow_nan=False, indent=2)
        except (TypeError, ValueError) as exc:
            raise ToolArgumentError() from exc
        if len(preview.encode("utf-8")) > 16 * 1024:
            raise ToolArgumentError()
        digest = hashlib.sha256((self.definition.name + "\0" + preview).encode()).hexdigest()
        return McpApprovalRequest(
            call_id, self.definition.name, "调用 MCP 工具", self.server.name,
            self.name, preview, self.server.transport == "streamable_http",
            MCP_APPROVAL_WARNING_TEXT, digest,
        )

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        try:
            async with asyncio.timeout(30):
                result = await self._client.call_tool(self.name, dict(arguments), read_timeout_seconds=25)
            if result.is_error:
                raise ToolRejectedError(ToolErrorCode.MCP_FAILED)
            content = [item.text for item in result.content if item.type == "text"]
            value = {"content": content}
            if result.structured_content is not None:
                value["structured_content"] = result.structured_content
            if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_RESULT_BYTES - 128:
                raise ToolRejectedError(ToolErrorCode.MCP_FAILED)
            return value
        except ToolRejectedError:
            raise
        except Exception as exc:
            raise ToolRejectedError(ToolErrorCode.MCP_FAILED) from exc


class McpRegistry:
    """一次发送的连接和工具定义在模型首步前固定，结束时关闭。"""

    def __init__(
        self,
        base_factory: Callable[[Sequence[McpTool]], ToolRuntime],
        configs: Sequence[McpServerConfig],
    ) -> None:
        self._base_factory = base_factory
        self._configs = tuple(configs)
        self._registry = base_factory(())
        self._stack: AsyncExitStack | None = None
        self._redaction_token = None
        self.request_id = ""
        self.unavailable_servers: tuple[str, ...] = ()

    @property
    def definitions(self):
        return self._registry.definitions

    async def execute(self, call, context=None):
        return await self._registry.execute(call, context)

    async def __aenter__(self):
        stack = AsyncExitStack()
        try:
            tools: list[McpTool] = []
            unavailable: list[str] = []
            schema_bytes = 0
            for config in self._configs:
                started = time.monotonic()
                first_tool = len(tools)
                try:
                    client = await self._connect(stack, config)
                    cursor = None
                    seen_cursors: set[str] = set()
                    for _ in range(10):
                        async with asyncio.timeout(10):
                            page = await client.list_tools(cursor=cursor)
                        for item in page.tools:
                            if len(tools) >= MAX_TOOLS:
                                break
                            tool = _adapt_tool(config, item, client)
                            if tool is not None and all(t.definition.name != tool.definition.name for t in tools):
                                size = len(json.dumps(tool.definition.parameters).encode("utf-8"))
                                if schema_bytes + size > MAX_TOTAL_SCHEMA_BYTES:
                                    continue
                                tools.append(tool)
                                schema_bytes += size
                        if not page.next_cursor or len(tools) >= MAX_TOOLS:
                            break
                        if page.next_cursor in seen_cursors:
                            raise ValueError("MCP tool pagination repeated")
                        seen_cursors.add(page.next_cursor)
                        cursor = page.next_cursor
                    else:
                        raise ValueError("MCP tool pagination exceeded limit")
                    _log_server_status(
                        request_id=self.request_id, server_name=config.name,
                        status="connected", duration_ms=round((time.monotonic() - started) * 1000, 2),
                    )
                except Exception:
                    # 一个外部服务不可用时，仍允许使用本地工具及其他 MCP Server。
                    del tools[first_tool:]
                    schema_bytes = sum(
                        len(json.dumps(tool.definition.parameters).encode("utf-8"))
                        for tool in tools
                    )
                    unavailable.append(config.name)
                    _log_server_status(
                        request_id=self.request_id, server_name=config.name,
                        status="unavailable", duration_ms=round((time.monotonic() - started) * 1000, 2),
                    )
                    continue
            self._registry = self._base_factory(tuple(tools))
            self.unavailable_servers = tuple(unavailable)
            self._stack = stack
            self._redaction_token = MCP_REDACT_LOGS.set(True)
            return self
        except BaseException:
            await stack.aclose()
            raise

    async def _connect(self, stack: AsyncExitStack, config: McpServerConfig) -> Client:
        if config.transport == "stdio":
            env = {key: os.environ[ref] for key, ref in config.env}
            params = StdioServerParameters(command=config.command or "", args=list(config.args), env=env)
            client = Client(params, read_timeout_seconds=10)
        else:
            headers = {key: os.environ[ref] for key, ref in config.headers}
            http = await stack.enter_async_context(
                httpx2.AsyncClient(headers=headers, timeout=10, follow_redirects=False)
            )
            client = Client(streamable_http_client(config.url or "", http_client=http), read_timeout_seconds=10)
        async with asyncio.timeout(10):
            return await stack.enter_async_context(client)

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self._stack is not None:
                await self._stack.aclose()
                self._stack = None
        finally:
            if self._redaction_token is not None:
                MCP_REDACT_LOGS.reset(self._redaction_token)
                self._redaction_token = None


def _adapt_tool(config: McpServerConfig, item: Any, client: Client) -> McpTool | None:
    if not isinstance(item.name, str) or not _TOOL_NAME.fullmatch(item.name):
        return None
    name = f"mcp_{config.name}_{item.name}"
    if len(name) > 64:
        return None
    schema = item.input_schema
    description = item.description or item.title or item.name
    if not isinstance(schema, dict) or schema.get("type") != "object" or not isinstance(description, str):
        return None
    schema = {"properties": {}, **schema}
    try:
        if (
            len(json.dumps(schema, ensure_ascii=False).encode("utf-8")) > MAX_ARGUMENT_BYTES
            or len(description.encode("utf-8")) > 2048
        ):
            return None
    except (TypeError, ValueError):
        return None
    tool = McpTool(config, item.name, ToolDefinition(name, description, schema, ToolEffect.EXECUTING), client)
    try:
        ToolRegistry((tool,))
    except (TypeError, ValueError):
        return None
    return tool


def _log_server_status(*, request_id: str, server_name: str, status: str, duration_ms: float) -> None:
    """只记录连接状态和耗时，不记录 Server 的错误或凭据。"""

    logging.getLogger("app.model_calls").info(
        "mcp_server",
        extra={
            "event": "mcp_server", "request_id": request_id,
            "server_name": server_name, "status": status,
            "duration_ms": duration_ms,
        },
    )
