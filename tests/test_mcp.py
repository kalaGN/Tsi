"""MCP 配置、工具审批和请求级连接验证。"""

import asyncio
import json
import logging
import sys

import pytest
import httpx2
from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from tools.contracts import ToolCall, ToolExecutionContext
from tools.mcp_client import McpRegistry, McpServerConfig, load_mcp_config
from tools.workspace import WorkspacePolicy, create_intent_workspace_registry
from app.observability.model_logging import LOGGER_NAME, log_model_tool_call, log_model_http_request
from app.webui.approvals import WebApprovalCoordinator, WebApprovalNotFound
from tools.contracts import MCP_APPROVAL_WARNING_TEXT, McpApprovalRequest


def test_config_accepts_explicit_transports_and_rejects_inline_credentials(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": [
        {"name": "local", "transport": "stdio", "command": sys.executable, "args": ["server.py"], "env": {"TOKEN": "MCP_TOKEN"}},
        {"name": "remote", "transport": "streamable_http", "url": "https://example.com/mcp", "headers": {"Authorization": "MCP_AUTH"}},
    ]}))
    configs = load_mcp_config(path)
    assert [item.name for item in configs] == ["local", "remote"]
    path.write_text(json.dumps({"servers": [{"name": "bad", "transport": "streamable_http", "url": "http://example.com/mcp", "headers": {"Authorization": "literal secret"}}]}))
    with pytest.raises(ValueError):
        load_mcp_config(path)
    path.unlink()
    path.symlink_to(tmp_path / "missing.json")
    with pytest.raises(ValueError):
        load_mcp_config(path)


class FakeClient:
    def __init__(self):
        self.calls = []

    async def list_tools(self, *, cursor=None):
        return types.ListToolsResult(tools=[types.Tool(
            name="echo", description="Echo", inputSchema={"type": "object", "properties": {"text": {"type": "string"}}},
        )])

    async def call_tool(self, name, arguments, read_timeout_seconds=None):
        self.calls.append((name, arguments))
        return types.CallToolResult(content=[types.TextContent(type="text", text=arguments["text"])])


def test_mcp_tools_are_disclosed_only_after_activation_and_approved(tmp_path, monkeypatch):
    async def run():
        client = FakeClient()

        async def connect(self, stack, config):
            return client

        monkeypatch.setattr(McpRegistry, "_connect", connect)
        policy = WorkspacePolicy(tmp_path)
        registry = McpRegistry(
            lambda mcp_tools: create_intent_workspace_registry(policy, mcp_tools=mcp_tools),
            (McpServerConfig("local", "stdio", command=sys.executable),),
        )
        async with registry:
            assert [definition.name for definition in registry.definitions] == ["activate_tool_groups"]
            await registry.execute(ToolCall("activate", "activate_tool_groups", '{"groups":["mcp"]}'))
            assert "mcp_local_echo" in [definition.name for definition in registry.definitions]
            call = ToolCall("call", "mcp_local_echo", '{"text":"hello"}')
            denied = await registry.execute(call, ToolExecutionContext(approval_handler=_deny))
            assert denied.is_error and client.calls == []
            approvals = []

            async def approve(request):
                approvals.append(request)
                return True

            result = await registry.execute(call, ToolExecutionContext(approval_handler=approve))
            assert not result.is_error
            assert json.loads(result.output)["data"] == {"content": ["hello"]}
            assert client.calls == [("echo", {"text": "hello"})]
            assert approvals[0].server_name == "local"

    asyncio.run(run())

async def _deny(_request):
    return False


def test_unavailable_server_keeps_local_tools(tmp_path, monkeypatch):
    async def run():
        async def unavailable(self, stack, config):
            raise OSError("private connection detail")

        monkeypatch.setattr(McpRegistry, "_connect", unavailable)
        policy = WorkspacePolicy(tmp_path)
        registry = McpRegistry(
            lambda mcp_tools: create_intent_workspace_registry(policy, mcp_tools=mcp_tools),
            (McpServerConfig("broken", "stdio", command=sys.executable),),
        )
        async with registry:
            assert registry.unavailable_servers == ("broken",)
            result = await registry.execute(ToolCall("activate", "activate_tool_groups", '{"groups":["general"]}'))
            assert not result.is_error
            assert "get_current_time" in [item.name for item in registry.definitions]

    asyncio.run(run())


def test_missing_referenced_environment_keeps_server_unavailable(tmp_path, monkeypatch):
    async def run():
        monkeypatch.delenv("MCP_MISSING_TEST_TOKEN", raising=False)
        policy = WorkspacePolicy(tmp_path)
        registry = McpRegistry(
            lambda mcp_tools: create_intent_workspace_registry(policy, mcp_tools=mcp_tools),
            (McpServerConfig("local", "stdio", command=sys.executable, env=(("TOKEN", "MCP_MISSING_TEST_TOKEN"),)),),
        )
        async with registry:
            assert registry.unavailable_servers == ("local",)

    asyncio.run(run())


def test_invalid_schema_is_not_disclosed_and_timeout_is_safe(tmp_path, monkeypatch):
    async def run():
        class MixedClient(FakeClient):
            async def list_tools(self, *, cursor=None):
                return types.ListToolsResult(tools=[
                    types.Tool(name="invalid", inputSchema={"type": "object", "properties": {}, "required": ["missing"]}),
                    types.Tool(name="timeout", inputSchema={"type": "object", "properties": {}}),
                ])

            async def call_tool(self, name, arguments, read_timeout_seconds=None):
                raise TimeoutError("private server failure")

        async def connect(self, stack, config):
            return MixedClient()

        monkeypatch.setattr(McpRegistry, "_connect", connect)
        policy = WorkspacePolicy(tmp_path)
        registry = McpRegistry(
            lambda mcp_tools: create_intent_workspace_registry(policy, mcp_tools=mcp_tools),
            (McpServerConfig("local", "stdio", command=sys.executable),),
        )
        async with registry:
            await registry.execute(ToolCall("activate", "activate_tool_groups", '{"groups":["mcp"]}'))
            names = [item.name for item in registry.definitions]
            assert "mcp_local_invalid" not in names
            assert "mcp_local_timeout" in names
            result = await registry.execute(
                ToolCall("timeout", "mcp_local_timeout", "{}"),
                ToolExecutionContext(approval_handler=_approve),
            )
            assert result.is_error
            assert "private server failure" not in result.output

    asyncio.run(run())


def test_web_mcp_approval_is_bound_to_request_and_expires():
    async def run():
        coordinator = WebApprovalCoordinator()
        events = []
        preview = McpApprovalRequest(
            "call", "mcp_local_echo", "调用 MCP 工具", "local", "echo",
            '{"text":"hello"}', False, MCP_APPROVAL_WARNING_TEXT, "a" * 64,
        )
        pending = asyncio.create_task(coordinator.request("request", preview, events.append))
        await asyncio.sleep(0)
        assert "local" in events[0]["paths"][0]
        assert "hello" in events[0]["diff"]
        coordinator.resolve(events[0]["approval_id"], "request", False)
        assert await pending is False
        with pytest.raises(WebApprovalNotFound):
            coordinator.resolve(events[0]["approval_id"], "request", True)

    asyncio.run(run())


def test_stdio_server_discovery_and_call(tmp_path):
    async def run():
        script = tmp_path / "server.py"
        script.write_text(
            "from mcp.server.mcpserver import MCPServer\n"
            "server = MCPServer('test')\n"
            "@server.tool()\n"
            "def echo(text: str) -> str:\n"
            "    return text\n"
            "server.run(transport='stdio')\n"
        )
        policy = WorkspacePolicy(tmp_path)
        registry = McpRegistry(
            lambda mcp_tools: create_intent_workspace_registry(policy, mcp_tools=mcp_tools),
            (McpServerConfig("local", "stdio", command=sys.executable, args=(str(script),)),),
        )
        async with registry:
            await registry.execute(ToolCall("activate", "activate_tool_groups", '{"groups":["mcp"]}'))
            assert "mcp_local_echo" in [item.name for item in registry.definitions]
            result = await registry.execute(ToolCall("echo", "mcp_local_echo", '{"text":"working"}'), ToolExecutionContext(approval_handler=_approve))
            assert not result.is_error
            assert "working" in result.output

    asyncio.run(run())


def test_cancelled_stdio_call_closes_request(tmp_path):
    async def run():
        script = tmp_path / "slow_server.py"
        script.write_text(
            "import time\n"
            "from mcp.server.mcpserver import MCPServer\n"
            "server = MCPServer('test')\n"
            "@server.tool()\n"
            "def slow() -> str:\n"
            "    time.sleep(10)\n"
            "    return 'done'\n"
            "server.run(transport='stdio')\n"
        )
        policy = WorkspacePolicy(tmp_path)
        registry = McpRegistry(
            lambda mcp_tools: create_intent_workspace_registry(policy, mcp_tools=mcp_tools),
            (McpServerConfig("local", "stdio", command=sys.executable, args=(str(script),)),),
        )
        async with registry:
            await registry.execute(ToolCall("activate", "activate_tool_groups", '{"groups":["mcp"]}'))
            pending = asyncio.create_task(registry.execute(
                ToolCall("slow", "mcp_local_slow", "{}"),
                ToolExecutionContext(approval_handler=_approve),
            ))
            await asyncio.sleep(0.1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

    asyncio.run(run())

async def _approve(_request):
    return True


def test_mcp_request_redacts_tool_and_model_log_content(tmp_path, monkeypatch):
    async def run():
        client = FakeClient()

        async def connect(self, stack, config):
            return client

        monkeypatch.setattr(McpRegistry, "_connect", connect)
        policy = WorkspacePolicy(tmp_path)
        registry = McpRegistry(
            lambda mcp_tools: create_intent_workspace_registry(policy, mcp_tools=mcp_tools),
            (McpServerConfig("local", "stdio", command=sys.executable),),
        )
        records = []

        class Recorder(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Recorder()
        logger = logging.getLogger(LOGGER_NAME)
        logger.addHandler(handler)
        try:
            async with registry:
                log_model_tool_call(request_id="id", call_id="call", tool_name="mcp_local_echo", arguments_chars=16, arguments_json='{"secret":"sensitive"}')
                log_model_http_request(request_id="id", provider="fake", model="fake", method="POST", url="https://example.com", request_body={"sensitive": "data"}, timeout={})
            log_model_tool_call(request_id="id", call_id="normal", tool_name="get_current_time", arguments_chars=3, arguments_json="safe")
        finally:
            logger.removeHandler(handler)
        assert "sensitive" not in str(records[1].__dict__)
        assert "sensitive" not in str(records[2].__dict__)
        assert records[-1].arguments_json == "safe"

    asyncio.run(run())


def test_streamable_http_server_discovery_and_call(tmp_path, monkeypatch):
    async def run():
        server = MCPServer("test")

        @server.tool()
        def echo(text: str) -> str:
            return text

        app = server.streamable_http_app(
            stateless_http=True,
            transport_security=TransportSecuritySettings(allowed_hosts=["127.0.0.1"]),
        )
        original_client = httpx2.AsyncClient

        def local_client(**kwargs):
            return original_client(transport=httpx2.ASGITransport(app=app), **kwargs)

        monkeypatch.setattr(httpx2, "AsyncClient", local_client)
        policy = WorkspacePolicy(tmp_path)
        registry = McpRegistry(
            lambda mcp_tools: create_intent_workspace_registry(policy, mcp_tools=mcp_tools),
            (McpServerConfig("remote", "streamable_http", url="http://127.0.0.1/mcp"),),
        )
        async with app.router.lifespan_context(app):
            async with registry:
                await registry.execute(ToolCall("activate", "activate_tool_groups", '{"groups":["mcp"]}'))
                assert "mcp_remote_echo" in [item.name for item in registry.definitions]
                result = await registry.execute(ToolCall("echo", "mcp_remote_echo", '{"text":"http works"}'), ToolExecutionContext(approval_handler=_approve))
                assert not result.is_error
                assert "http works" in result.output

    asyncio.run(run())
