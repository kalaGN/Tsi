import asyncio
import hashlib
import io
import json
import logging

import httpx
import pytest

from app.observability import model_logging
from app.runtime.chat import run_chat_messages
from app.runtime.tool_loop import WORKSPACE_TOOL_LOOP_LIMITS
from app.services.llm.aliyun import (
    ALIYUN_RESPONSES_URL,
    AliyunResponsesProvider,
)
from app.services.llm.contracts import (
    ChatMessage,
    ChatRole,
    LlmProviderError,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderInvalidResponseError,
    ProviderInvalidRequestError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from app.services.llm.deepseek import (
    DEEPSEEK_CHAT_COMPLETIONS_URL,
    DeepSeekChatProvider,
)
from app.services.llm.factory import create_provider, resolve_provider_config
from app.services.llm import deepseek, http_client
from app.services.llm.http_client import post_json, post_sse
from tools.contracts import ToolDefinition, ToolResult
from tools.workspace import WorkspacePolicy, create_workspace_registry


FAKE_API_KEY = "test-only-provider-key"
REQUEST_ID = "0123456789abcdef0123456789abcdef"
TIME_TOOL = ToolDefinition(
    name="get_current_time",
    description="Get current time for a timezone",
    parameters={
        "type": "object",
        "properties": {"timezone": {"type": "string"}},
        "required": ["timezone"],
    },
)


def user_messages(content: str = "hello") -> tuple[ChatMessage, ...]:
    return (ChatMessage(ChatRole.USER, content),)


def install_transport(monkeypatch, handler, *, adapt_streaming_json=True):
    """让 Provider 使用内存 Transport，确保测试不会访问真实模型。"""

    real_async_client = httpx.AsyncClient

    def adapted_handler(request):
        response = handler(request)
        if adapt_streaming_json:
            return _adapt_json_response_to_sse(request, response)
        return response

    transport = httpx.MockTransport(adapted_handler)

    def create_client(**kwargs):
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(http_client.httpx, "AsyncClient", create_client)


def _adapt_json_response_to_sse(request, response):
    """把旧行为测试的成功 JSON 转成单事件流，避免掩盖生产协议分支。"""

    if (
        request.headers.get("Accept") != "text/event-stream"
        or not response.is_success
        or response.headers.get("content-type", "").startswith("text/event-stream")
    ):
        return response
    try:
        body = json.loads(response.content)
    except (TypeError, ValueError):
        return response

    if str(request.url) == DEEPSEEK_CHAT_COMPLETIONS_URL:
        events = _deepseek_json_as_sse(body)
    elif str(request.url) == ALIYUN_RESPONSES_URL:
        events = _aliyun_json_as_sse(body)
    else:
        return response
    return httpx.Response(
        response.status_code,
        headers={"content-type": "text/event-stream"},
        stream=ChunkedAsyncStream((events,)),
    )


def _deepseek_json_as_sse(body):
    """把完整 DeepSeek message 变成协议等价的单个 Delta。"""

    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices:
        chunk = body
    else:
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        if not isinstance(message, dict):
            chunk = body
        else:
            delta = dict(message)
            raw_calls = delta.get("tool_calls")
            if isinstance(raw_calls, list):
                delta["tool_calls"] = [
                    {"index": index, **raw_call}
                    for index, raw_call in enumerate(raw_calls)
                ]
            chunk = {
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": "tool_calls" if raw_calls else "stop",
                    }
                ]
            }
    return (
        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        "data: [DONE]\n\n"
    ).encode()


def _aliyun_json_as_sse(body):
    """把完整 Responses 对象变成 Delta 与 completed 测试事件。"""

    response_body = dict(body) if isinstance(body, dict) else body
    events = []
    if isinstance(response_body, dict):
        response_body.setdefault("status", "completed")
        output_text = response_body.get("output_text")
        if isinstance(output_text, str) and output_text:
            events.append({"type": "response.output_text.delta", "delta": output_text})
    events.append({"type": "response.completed", "response": response_body})
    return b"".join(
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
        for event in events
    )


class ChunkedAsyncStream(httpx.AsyncByteStream):
    """按指定网络分块返回字节，覆盖 SSE 跨 Chunk 边界。"""

    def __init__(self, chunks):
        self._chunks = tuple(chunks)

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class BlockingAsyncStream(httpx.AsyncByteStream):
    """保持读取挂起，并暴露关闭状态用于超时和取消测试。"""

    def __init__(self):
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.started.set()
        await asyncio.Event().wait()
        yield b""  # pragma: no cover - 仅用于满足异步生成器契约

    async def aclose(self):
        self.closed = True


async def run_provider_once(provider, messages):
    """通过最终 Turn 契约执行一次不带工具的模型步骤。"""

    turn = provider.create_turn(messages, (), request_id=REQUEST_ID)
    return await turn.next()


def test_factory_defaults_to_deepseek_without_provider_setting():
    config = resolve_provider_config({"DEEPSEEK_API_KEY": FAKE_API_KEY})

    assert config.provider == "deepseek"
    assert config.model == "deepseek-v4-flash"
    assert config.api_key_configured is True
    assert FAKE_API_KEY not in repr(config)


def test_factory_normalizes_deepseek_and_model_override():
    config = resolve_provider_config(
        {
            "LLM_PROVIDER": "  DeepSeek ",
            "DEEPSEEK_API_KEY": FAKE_API_KEY,
            "DEEPSEEK_MODEL": " deepseek-v4-pro ",
        }
    )

    assert config.provider == "deepseek"
    assert config.model == "deepseek-v4-pro"
    assert config.api_key_configured is True


def test_factory_applies_aliyun_model_override():
    config = resolve_provider_config(
        {
            "LLM_PROVIDER": "aliyun",
            "DASHSCOPE_API_KEY": FAKE_API_KEY,
            "ALIYUN_MODEL": " custom-qwen ",
        }
    )

    assert config.provider == "aliyun"
    assert config.model == "custom-qwen"


@pytest.mark.parametrize("provider", ["", "   ", "unknown"])
def test_factory_rejects_blank_or_unknown_provider(provider):
    with pytest.raises(ProviderConfigurationError) as captured:
        resolve_provider_config({"LLM_PROVIDER": provider})

    assert captured.value.user_message == "Unsupported LLM provider configuration"


@pytest.mark.parametrize(
    ("environ", "expected_model"),
    [
        (
            {"LLM_PROVIDER": "aliyun", "ALIYUN_MODEL": "  "},
            "qwen3-max",
        ),
        (
            {"LLM_PROVIDER": "deepseek", "DEEPSEEK_MODEL": "  "},
            "deepseek-v4-flash",
        ),
    ],
)
def test_factory_uses_default_for_blank_model(environ, expected_model):
    assert resolve_provider_config(environ).model == expected_model


def test_factory_creates_selected_provider():
    aliyun = create_provider(
        {
            "LLM_PROVIDER": "aliyun",
            "DASHSCOPE_API_KEY": FAKE_API_KEY,
        }
    )
    deepseek = create_provider(
        {
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": FAKE_API_KEY,
        }
    )

    assert isinstance(aliyun, AliyunResponsesProvider)
    assert isinstance(deepseek, DeepSeekChatProvider)


@pytest.mark.parametrize(
    ("body", "expected_text"),
    [
        ({"output_text": "top-level"}, "top-level"),
        ({"output": [{"text": "first"}, {"text": "second"}]}, "first\nsecond"),
        (
            {
                "output": [
                    {"content": [{"text": "first"}, {"type": "metadata"}]},
                    {"content": [{"text": "second"}]},
                ]
            },
            "first\nsecond",
        ),
    ],
)
def test_aliyun_sends_expected_request_and_extracts_text(
    monkeypatch,
    body,
    expected_text,
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == ALIYUN_RESPONSES_URL
        assert request.headers["Authorization"] == f"Bearer {FAKE_API_KEY}"
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["Accept"] == "text/event-stream"
        assert request.content == (
            b'{"model":"custom-qwen","input":'
            b'[{"role":"user","content":"hello"}],"stream":true}'
        )
        return httpx.Response(200, json=body)

    install_transport(monkeypatch, handler)
    provider = AliyunResponsesProvider(FAKE_API_KEY, "custom-qwen")

    result = asyncio.run(run_provider_once(provider, user_messages()))

    assert result.upstream_status == 200
    assert result.output_text == expected_text


def test_aliyun_turn_declares_flat_tools_and_uses_array_input(monkeypatch):
    captured_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"output_text": "ok"})

    install_transport(monkeypatch, handler)
    turn = AliyunResponsesProvider(FAKE_API_KEY, "custom-qwen").create_turn(
        user_messages("hello"),
        (TIME_TOOL,),
        request_id=REQUEST_ID,
    )

    step = asyncio.run(turn.next())

    assert step.output_text == "ok"
    assert step.tool_calls == ()
    assert captured_payloads == [
        {
            "model": "custom-qwen",
            "input": [{"role": "user", "content": "hello"}],
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "get_current_time",
                    "description": "Get current time for a timezone",
                    "parameters": TIME_TOOL.parameters,
                }
            ],
            "tool_choice": "auto",
        }
    ]


@pytest.mark.parametrize(
    "provider",
    (
        AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"),
        DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
    ),
)
def test_both_providers_send_all_workspace_schemas_without_host_metadata(
    tmp_path,
    monkeypatch,
    provider,
):
    payloads = []
    definitions = create_workspace_registry(
        WorkspacePolicy(tmp_path)
    ).definitions

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if provider.name == "aliyun":
            return httpx.Response(200, json={"output_text": "ok"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    install_transport(monkeypatch, handler)
    turn = provider.create_turn(user_messages(), definitions, request_id=REQUEST_ID)
    assert asyncio.run(turn.next()).output_text == "ok"

    declared = payloads[0]["tools"]
    if provider.name == "deepseek":
        declared = [item["function"] for item in declared]
    assert {item["name"] for item in declared} == {
        definition.name for definition in definitions
    }
    assert len(declared) == 9
    assert all(
        set(item) == {"name", "description", "parameters"}
        or set(item) == {"type", "name", "description", "parameters"}
        for item in declared
    )
    serialized = json.dumps(payloads[0])
    assert "max_argument_bytes" not in serialized
    assert '"effect"' not in serialized

    # 上游必须看到 Edit 的真实字段；只声明 array 会诱导模型生成执行器不认识的别名。
    edit_schema = next(
        item["parameters"]
        for item in declared
        if item["name"] == "apply_workspace_edits"
    )
    edit_items = edit_schema["properties"]["edits"]["items"]
    assert edit_items["properties"]["mode"]["enum"] == ["create", "replace"]
    assert set(edit_items["properties"]) == {
        "mode",
        "path",
        "content",
        "expected_sha256",
        "old_text",
        "new_text",
    }
    assert edit_items["additionalProperties"] is False


@pytest.mark.parametrize(
    "provider",
    (
        AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"),
        DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
    ),
)
def test_both_providers_accept_workspace_edit_arguments_above_eight_kib(
    tmp_path,
    monkeypatch,
    provider,
):
    arguments = json.dumps(
        {
            "edits": [
                {"mode": "create", "path": "large.txt", "content": "中" * 4000}
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert 8 * 1024 < len(arguments.encode("utf-8")) < 64 * 1024

    def handler(_request: httpx.Request) -> httpx.Response:
        if provider.name == "aliyun":
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "function_call",
                            "id": "large-1",
                            "call_id": "large-1",
                            "name": "apply_workspace_edits",
                            "arguments": arguments,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "large-1",
                                    "type": "function",
                                    "function": {
                                        "name": "apply_workspace_edits",
                                        "arguments": arguments,
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    install_transport(monkeypatch, handler)
    definitions = create_workspace_registry(WorkspacePolicy(tmp_path)).definitions
    step = asyncio.run(
        provider.create_turn(
            user_messages(),
            definitions,
            request_id=REQUEST_ID,
        ).next()
    )

    assert step.tool_calls[0].name == "apply_workspace_edits"
    assert step.tool_calls[0].arguments_json == arguments


@pytest.mark.parametrize(
    "provider",
    (
        AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"),
        DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
    ),
)
def test_both_providers_complete_workspace_read_edit_check_loop(
    tmp_path,
    monkeypatch,
    provider,
):
    source = tmp_path / "中文.txt"
    source.write_text("旧内容\n", encoding="utf-8")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    python = tmp_path / ".venv" / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    calls = [
        ("read-1", "read_workspace_file", '{"path":"中文.txt"}'),
        (
            "write-1",
            "apply_workspace_edits",
            json.dumps(
                {
                    "edits": [
                        {
                            "mode": "replace",
                            "path": "中文.txt",
                            "expected_sha256": digest,
                            "old_text": "旧内容",
                            "new_text": "新内容",
                        }
                    ]
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
        ("check-1", "run_project_check", '{"name":"pip_check"}'),
    ]
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        index = len(payloads) - 1
        if index == len(calls):
            if provider.name == "aliyun":
                return httpx.Response(200, json={"output_text": "完成"})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "完成"}}]},
            )
        call_id, name, arguments = calls[index]
        if provider.name == "aliyun":
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "function_call",
                            "id": call_id,
                            "call_id": call_id,
                            "name": name,
                            "arguments": arguments,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": arguments,
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    async def approve(_request):
        return True

    install_transport(monkeypatch, handler)
    result = asyncio.run(
        run_chat_messages(
            user_messages("修改并检查"),
            provider=provider,
            registry=create_workspace_registry(WorkspacePolicy(tmp_path)),
            on_tool_approval=approve,
            tool_loop_limits=WORKSPACE_TOOL_LOOP_LIMITS,
        )
    )

    assert result.output_text == "完成"
    assert source.read_text(encoding="utf-8") == "新内容\n"
    assert len(payloads) == 4
    serialized_continuations = json.dumps(payloads[1:], ensure_ascii=False)
    assert "中文.txt" in serialized_continuations
    assert '"change_id"' in serialized_continuations
    nested_strings = []

    def collect_strings(value):
        if isinstance(value, str):
            nested_strings.append(value)
        elif isinstance(value, list):
            for item in value:
                collect_strings(item)
        elif isinstance(value, dict):
            for item in value.values():
                collect_strings(item)

    collect_strings(payloads)
    assert any(
        '"exit_code":0' in value.replace(" ", "")
        for value in nested_strings
    )


def test_aliyun_turn_appends_each_function_call_next_to_its_output(
    monkeypatch,
    captured_model_events,
):
    captured_payloads = []
    responses = iter(
        [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "get_current_time",
                        "arguments": '{"timezone":"Asia/Shanghai"}',
                        "call_id": "call-1",
                    },
                    {
                        "type": "function_call",
                        "name": "get_current_time",
                        "arguments": '{"timezone":"UTC"}',
                        "call_id": "call-2",
                    },
                ]
            },
            {"output_text": "done"},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        return httpx.Response(200, json=next(responses))

    install_transport(monkeypatch, handler)
    turn = AliyunResponsesProvider(FAKE_API_KEY).create_turn(
        (
            ChatMessage(ChatRole.SYSTEM, "project rules"),
            ChatMessage(ChatRole.USER, "hello"),
        ),
        (TIME_TOOL,),
        request_id=REQUEST_ID,
    )

    first_step = asyncio.run(turn.next())
    final_step = asyncio.run(
        turn.next(
            (
                ToolResult("call-1", '{"ok":true,"data":"first"}'),
                ToolResult("call-2", '{"ok":true,"data":"second"}'),
            )
        )
    )

    assert [call.call_id for call in first_step.tool_calls] == ["call-1", "call-2"]
    assert final_step.output_text == "done"
    for payload in captured_payloads:
        assert payload["input"][0] == {
            "role": "system",
            "content": "project rules",
        }
        assert sum(item.get("role") == "system" for item in payload["input"]) == 1
    assert captured_payloads[1]["input"][-4:] == [
        {
            "type": "function_call",
            "name": "get_current_time",
            "arguments": '{"timezone":"Asia/Shanghai"}',
            "call_id": "call-1",
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": '{"ok":true,"data":"first"}',
        },
        {
            "type": "function_call",
            "name": "get_current_time",
            "arguments": '{"timezone":"UTC"}',
            "call_id": "call-2",
        },
        {
            "type": "function_call_output",
            "call_id": "call-2",
            "output": '{"ok":true,"data":"second"}',
        },
    ]
    request_bodies = [
        event["request_body"]
        for event in captured_model_events()
        if event["event"] == "llm_http_request"
    ]
    assert request_bodies == captured_payloads


def test_aliyun_streams_text_deltas_and_validates_completed_response(monkeypatch):
    events = (
        {"type": "response.output_text.delta", "delta": "你"},
        # 阿里云真实流可能在有效文本后发送空 delta；它不应中断整轮响应。
        {"type": "response.output_text.delta", "delta": ""},
        {"type": "response.output_text.delta", "delta": "好"},
        {"type": "response.output_text.done", "text": "你好"},
        {
            "type": "response.completed",
            "response": {"status": "completed", "output_text": "你好"},
        },
    )
    chunks = tuple(
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
        for event in events
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkedAsyncStream(chunks),
        )

    install_transport(monkeypatch, handler, adapt_streaming_json=False)
    turn = AliyunResponsesProvider(FAKE_API_KEY).create_turn(
        user_messages(),
        (),
        request_id=REQUEST_ID,
    )
    deltas = []

    step = asyncio.run(turn.next(on_text_delta=deltas.append))

    assert deltas == ["你", "好"]
    assert step.output_text == "你好"
    assert step.tool_calls == ()


def test_aliyun_stream_rejects_non_string_text_delta(monkeypatch):
    body = b'data:{"type":"response.output_text.delta","delta":null}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkedAsyncStream((body,)),
        )

    install_transport(monkeypatch, handler, adapt_streaming_json=False)
    turn = AliyunResponsesProvider(FAKE_API_KEY).create_turn(
        user_messages(),
        (),
        request_id=REQUEST_ID,
    )

    with pytest.raises(ProviderInvalidResponseError):
        asyncio.run(turn.next())


def test_aliyun_stream_validates_tool_argument_deltas(monkeypatch):
    call_item = {
        "id": "item-1",
        "type": "function_call",
        "name": "get_current_time",
        "arguments": '{"timezone":"UTC"}',
        "call_id": "call-1",
    }
    events = (
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item-1",
            "delta": '{"time',
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item-1",
            "delta": 'zone":"UTC"}',
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "item-1",
            "arguments": '{"timezone":"UTC"}',
        },
        {"type": "response.output_item.done", "item": call_item},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [call_item],
            },
        },
    )
    body = b"".join(
        f"data: {json.dumps(event)}\n\n".encode() for event in events
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkedAsyncStream((body,)),
        )

    install_transport(monkeypatch, handler, adapt_streaming_json=False)
    turn = AliyunResponsesProvider(FAKE_API_KEY).create_turn(
        user_messages(),
        (TIME_TOOL,),
        request_id=REQUEST_ID,
    )

    step = asyncio.run(turn.next())

    assert len(step.tool_calls) == 1
    assert step.tool_calls[0].arguments_json == '{"timezone":"UTC"}'


def test_deepseek_sends_expected_request_and_extracts_text(monkeypatch):
    body = {
        "id": "chat-1",
        "choices": [{"message": {"role": "assistant", "content": "你好"}}],
        "usage": {"total_tokens": 3},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == DEEPSEEK_CHAT_COMPLETIONS_URL
        assert request.headers["Authorization"] == f"Bearer {FAKE_API_KEY}"
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["Accept"] == "text/event-stream"
        assert request.content == (
            b'{"model":"deepseek-v4-pro","messages":'
            b'[{"role":"user","content":"\xe4\xbd\xa0\xe5\xa5\xbd"}],"stream":true}'
        )
        return httpx.Response(200, json=body)

    install_transport(monkeypatch, handler)
    provider = DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-pro")

    result = asyncio.run(run_provider_once(provider, user_messages("你好")))

    assert result.upstream_status == 200
    assert result.output_text == "你好"


def test_deepseek_turn_declares_tools_and_returns_direct_text(monkeypatch):
    captured_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    install_transport(monkeypatch, handler)
    turn = DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-pro").create_turn(
        user_messages("hello"),
        (TIME_TOOL,),
        request_id=REQUEST_ID,
    )

    step = asyncio.run(turn.next())

    assert step.output_text == "ok"
    assert step.tool_calls == ()
    assert captured_payloads == [
        {
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_current_time",
                        "description": "Get current time for a timezone",
                        "parameters": TIME_TOOL.parameters,
                    },
                }
            ],
            "tool_choice": "auto",
        }
    ]


def test_deepseek_turn_continues_with_assistant_and_ordered_tool_results(
    monkeypatch,
    captured_model_events,
):
    captured_payloads = []
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "checking",
                            "reasoning_content": "must be returned",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_current_time",
                                        "arguments": '{"timezone":"Asia/Shanghai"}',
                                    },
                                },
                                {
                                    "id": "call-2",
                                    "type": "function",
                                    "function": {
                                        "name": "get_current_time",
                                        "arguments": '{"timezone":"UTC"}',
                                    },
                                },
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "done"}}
                ]
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        return httpx.Response(200, json=next(responses))

    install_transport(monkeypatch, handler)
    turn = DeepSeekChatProvider(FAKE_API_KEY).create_turn(
        (
            ChatMessage(ChatRole.SYSTEM, "project rules"),
            ChatMessage(ChatRole.USER, "hello"),
        ),
        (TIME_TOOL,),
        request_id=REQUEST_ID,
    )

    first_step = asyncio.run(turn.next())
    final_step = asyncio.run(
        turn.next(
            (
                ToolResult("call-1", '{"ok":true,"data":"first"}'),
                ToolResult("call-2", '{"ok":true,"data":"second"}'),
            )
        )
    )

    assert [call.call_id for call in first_step.tool_calls] == ["call-1", "call-2"]
    assert first_step.output_text == "checking"
    assert final_step.output_text == "done"
    for payload in captured_payloads:
        assert payload["messages"][0] == {
            "role": "system",
            "content": "project rules",
        }
        assert sum(
            item.get("role") == "system" for item in payload["messages"]
        ) == 1
    continued_messages = captured_payloads[1]["messages"]
    assert continued_messages[-3] == {
        "role": "assistant",
        "content": "checking",
        "reasoning_content": "must be returned",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "get_current_time",
                    "arguments": '{"timezone":"Asia/Shanghai"}',
                },
            },
            {
                "id": "call-2",
                "type": "function",
                "function": {
                    "name": "get_current_time",
                    "arguments": '{"timezone":"UTC"}',
                },
            },
        ],
    }
    assert continued_messages[-2:] == [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"ok":true,"data":"first"}',
        },
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "content": '{"ok":true,"data":"second"}',
        },
    ]
    request_bodies = [
        event["request_body"]
        for event in captured_model_events()
        if event["event"] == "llm_http_request"
    ]
    assert request_bodies == captured_payloads


def test_deepseek_streams_text_deltas_in_order(monkeypatch):
    chunks = (
        b'data: {"choices":[{"index":0,"delta":{"role":"assistant",'
        b'"content":"\xe4\xbd\xa0"},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{"content":"\xe5\xa5\xbd"},'
        b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkedAsyncStream(chunks),
        )

    install_transport(monkeypatch, handler, adapt_streaming_json=False)
    turn = DeepSeekChatProvider(FAKE_API_KEY).create_turn(
        user_messages(),
        (),
        request_id=REQUEST_ID,
    )
    deltas = []

    step = asyncio.run(turn.next(on_text_delta=deltas.append))

    assert deltas == ["你", "好"]
    assert step.output_text == "你好"
    assert step.tool_calls == ()


def test_deepseek_stream_assembles_tool_arguments_across_chunks(monkeypatch):
    chunks = (
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        b'"id":"call-1","type":"function","function":{"name":"get_current_time",'
        b'"arguments":"{\\\"time"}}]},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        b'"function":{"arguments":"zone\\\":\\\"UTC\\\"}"}}]},'
        b'"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n',
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkedAsyncStream(chunks),
        )

    install_transport(monkeypatch, handler, adapt_streaming_json=False)
    turn = DeepSeekChatProvider(FAKE_API_KEY).create_turn(
        user_messages(),
        (TIME_TOOL,),
        request_id=REQUEST_ID,
    )

    step = asyncio.run(turn.next())

    assert step.output_text is None
    assert len(step.tool_calls) == 1
    assert step.tool_calls[0].call_id == "call-1"
    assert step.tool_calls[0].name == "get_current_time"
    assert step.tool_calls[0].arguments_json == '{"timezone":"UTC"}'


def test_deepseek_stream_bounds_private_reasoning_content(monkeypatch):
    body = (
        b'data: {"choices":[{"index":0,"delta":'
        b'{"reasoning_content":"hidden"},"finish_reason":null}]}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkedAsyncStream((body,)),
        )

    install_transport(monkeypatch, handler, adapt_streaming_json=False)
    monkeypatch.setattr(deepseek, "MAX_STREAM_OUTPUT_BYTES", 3)
    turn = DeepSeekChatProvider(FAKE_API_KEY).create_turn(
        user_messages(),
        (),
        request_id=REQUEST_ID,
    )

    with pytest.raises(ProviderInvalidResponseError):
        asyncio.run(turn.next())


def test_deepseek_stream_bounds_tool_call_count_before_runtime(monkeypatch):
    raw_calls = [
        {
            "index": index,
            "id": f"call-{index}",
            "type": "function",
            "function": {"name": "get_current_time", "arguments": "{}"},
        }
        for index in range(2)
    ]
    event = {
        "choices": [
            {
                "index": 0,
                "delta": {"tool_calls": raw_calls},
                "finish_reason": "tool_calls",
            }
        ]
    }
    body = (
        f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode()
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkedAsyncStream((body,)),
        )

    install_transport(monkeypatch, handler, adapt_streaming_json=False)
    monkeypatch.setattr(deepseek, "MAX_STREAM_TOOL_CALLS", 1)
    turn = DeepSeekChatProvider(FAKE_API_KEY).create_turn(
        user_messages(),
        (TIME_TOOL,),
        request_id=REQUEST_ID,
    )

    with pytest.raises(ProviderInvalidResponseError):
        asyncio.run(turn.next())


def test_deepseek_turn_rejects_mismatched_tool_result_before_second_request(
    monkeypatch,
):
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_current_time",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    install_transport(monkeypatch, handler)
    turn = DeepSeekChatProvider(FAKE_API_KEY).create_turn(
        user_messages(),
        (TIME_TOOL,),
        request_id=REQUEST_ID,
    )
    asyncio.run(turn.next())

    with pytest.raises(ProviderInvalidRequestError):
        asyncio.run(turn.next((ToolResult("wrong-id", "{}"),)))

    assert request_count == 1


@pytest.mark.parametrize(
    ("provider", "body"),
    [
        (
            DeepSeekChatProvider(FAKE_API_KEY),
            {"choices": [{"message": {"tool_calls": "invalid"}}]},
        ),
        (
            AliyunResponsesProvider(FAKE_API_KEY),
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "get_current_time",
                        "arguments": "{}",
                    }
                ]
            },
        ),
    ],
)
def test_provider_turn_rejects_malformed_tool_call(monkeypatch, provider, body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    install_transport(monkeypatch, handler)
    turn = provider.create_turn(
        user_messages(),
        (TIME_TOOL,),
        request_id=REQUEST_ID,
    )

    with pytest.raises(ProviderInvalidResponseError):
        asyncio.run(turn.next())


@pytest.mark.parametrize(
    ("provider", "expected_field"),
    [
        (AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"), "input"),
        (
            DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
            "messages",
        ),
    ],
)
def test_provider_sends_ordered_multi_turn_messages(
    monkeypatch,
    provider,
    expected_field,
):
    history = (
        ChatMessage(ChatRole.USER, "first"),
        ChatMessage(ChatRole.ASSISTANT, "answer"),
        ChatMessage(ChatRole.USER, "second"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload[expected_field] == [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ]
        if provider.name == "aliyun":
            return httpx.Response(200, json={"output_text": "ok"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    install_transport(monkeypatch, handler)

    assert asyncio.run(run_provider_once(provider, history)).output_text == "ok"


@pytest.mark.parametrize(
    ("provider", "expected_field"),
    [
        (AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"), "input"),
        (
            DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
            "messages",
        ),
    ],
)
def test_provider_sends_single_system_message_before_conversation(
    monkeypatch,
    provider,
    expected_field,
):
    history = (
        ChatMessage(ChatRole.SYSTEM, "项目规则"),
        ChatMessage(ChatRole.USER, "first"),
        ChatMessage(ChatRole.ASSISTANT, "answer"),
        ChatMessage(ChatRole.USER, "second"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload[expected_field] == [
            {"role": "system", "content": "项目规则"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ]
        if provider.name == "aliyun":
            return httpx.Response(200, json={"output_text": "ok"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    install_transport(monkeypatch, handler)

    assert asyncio.run(run_provider_once(provider, history)).output_text == "ok"


@pytest.mark.parametrize(
    "messages",
    [
        (),
        (ChatMessage(ChatRole.USER, ""),),
        (ChatMessage(ChatRole.ASSISTANT, "orphan"),),
        (ChatMessage(ChatRole.SYSTEM, "rules"),),
        (
            ChatMessage(ChatRole.SYSTEM, ""),
            ChatMessage(ChatRole.USER, "question"),
        ),
        (
            ChatMessage(ChatRole.SYSTEM, "first"),
            ChatMessage(ChatRole.SYSTEM, "second"),
            ChatMessage(ChatRole.USER, "question"),
        ),
        (
            ChatMessage(ChatRole.USER, "question"),
            ChatMessage(ChatRole.SYSTEM, "late rules"),
            ChatMessage(ChatRole.USER, "second"),
        ),
        (
            ChatMessage(ChatRole.USER, "first"),
            ChatMessage(ChatRole.USER, "second"),
            ChatMessage(ChatRole.USER, "third"),
        ),
    ],
)
def test_provider_rejects_invalid_message_sequence_without_network(
    monkeypatch,
    messages,
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid messages must not reach network")

    install_transport(monkeypatch, handler)
    provider = DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash")

    with pytest.raises(ProviderInvalidRequestError) as captured:
        asyncio.run(run_provider_once(provider, messages))

    assert str(captured.value) == "Conversation messages are invalid"


@pytest.mark.parametrize(
    "provider",
    [
        AliyunResponsesProvider("", "qwen3-max"),
        DeepSeekChatProvider("   ", "deepseek-v4-flash"),
    ],
)
def test_provider_requires_key_before_network_call(monkeypatch, provider):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called without a key")

    install_transport(monkeypatch, handler)

    with pytest.raises(ProviderConfigurationError) as captured:
        asyncio.run(run_provider_once(provider, user_messages()))

    assert captured.value.user_message == "Upstream API key is not configured"


@pytest.mark.parametrize(
    "provider",
    [
        AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"),
        DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
    ],
)
def test_provider_maps_timeout(monkeypatch, provider):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret timeout detail", request=request)

    install_transport(monkeypatch, handler)

    with pytest.raises(ProviderTimeoutError) as captured:
        asyncio.run(run_provider_once(provider, user_messages()))

    assert captured.value.user_message == "Upstream request timed out"
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize(
    "provider",
    [
        AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"),
        DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
    ],
)
def test_provider_maps_connection_error(monkeypatch, provider):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret connection detail", request=request)

    install_transport(monkeypatch, handler)

    with pytest.raises(ProviderConnectionError) as captured:
        asyncio.run(run_provider_once(provider, user_messages()))

    assert captured.value.user_message == "Unable to connect to upstream service"
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize("status_code", [401, 403])
def test_provider_maps_authentication_error(monkeypatch, status_code):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "secret upstream body"})

    install_transport(monkeypatch, handler)
    provider = DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash")

    with pytest.raises(ProviderAuthenticationError) as captured:
        asyncio.run(run_provider_once(provider, user_messages()))

    assert captured.value.status_code == status_code
    assert captured.value.user_message == "Upstream authentication failed"
    assert "secret" not in str(captured.value)
    assert FAKE_API_KEY not in str(captured.value)


@pytest.mark.parametrize("status_code", [302, 400, 402, 422, 429, 500, 503])
def test_deepseek_preserves_other_error_status(monkeypatch, status_code):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "secret upstream body"})

    install_transport(monkeypatch, handler)
    provider = DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash")

    with pytest.raises(ProviderResponseError) as captured:
        asyncio.run(run_provider_once(provider, user_messages()))

    assert captured.value.status_code == status_code
    assert captured.value.user_message == "Upstream service returned an error"
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize(
    "provider",
    [
        AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"),
        DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
    ],
)
def test_provider_rejects_non_json_success(monkeypatch, provider):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    install_transport(monkeypatch, handler)

    with pytest.raises(ProviderInvalidResponseError) as captured:
        asyncio.run(run_provider_once(provider, user_messages()))

    assert captured.value.user_message == "Upstream service returned an invalid response"


@pytest.mark.parametrize(
    ("provider", "body"),
    [
        (AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"), {"output": []}),
        (DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"), {}),
        (
            DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
            {"choices": []},
        ),
        (
            DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
            {"choices": [{"message": {"content": None}}]},
        ),
        (
            DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
            {"choices": [{"message": {"content": ""}}]},
        ),
    ],
)
def test_provider_rejects_success_without_text(monkeypatch, provider, body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    install_transport(monkeypatch, handler, adapt_streaming_json=False)

    with pytest.raises(ProviderInvalidResponseError) as captured:
        asyncio.run(run_provider_once(provider, user_messages()))

    assert captured.value.user_message == "Upstream service returned an invalid response"


@pytest.fixture
def captured_model_events(tmp_path):
    """配置模型日志到内存流，返回解析事件并在测试后还原 Logger。"""

    stream = io.StringIO()
    log_path = tmp_path / "model-calls.log"
    logger = logging.getLogger(model_logging.LOGGER_NAME)
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    for handler in original_handlers:
        logger.removeHandler(handler)
    model_logging.configure_model_logging(stream=stream, log_path=log_path)

    def read_events():
        return [json.loads(line) for line in stream.getvalue().splitlines()]

    yield read_events

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler in original_handlers:
        logger.addHandler(handler)
    logger.setLevel(original_level)
    logger.propagate = original_propagate


def _deepseek_ok_body() -> dict:
    return {"choices": [{"message": {"content": "ok"}}]}


def test_post_json_logs_request_and_response_with_controllable_clock(
    monkeypatch, captured_model_events
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_deepseek_ok_body())

    install_transport(monkeypatch, handler)
    ticks = iter([100.0, 100.25])

    status, body = asyncio.run(
        post_json(
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            FAKE_API_KEY,
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            request_id=REQUEST_ID,
            provider="deepseek",
            model="m",
            clock=lambda: next(ticks),
        )
    )

    assert status == 200
    assert body == _deepseek_ok_body()
    events = captured_model_events()
    assert [event["event"] for event in events] == [
        "llm_http_request",
        "llm_http_response",
    ]

    request_event = events[0]
    assert request_event["request_body"] == {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert request_event["method"] == "POST"
    assert request_event["url"] == DEEPSEEK_CHAT_COMPLETIONS_URL
    assert request_event["timeout"] == {
        "connect_seconds": 10.0,
        "total_seconds": 60.0,
    }
    assert request_event["headers"]["Authorization"] == "Bearer [REDACTED]"

    response_event = events[1]
    assert response_event["status_code"] == 200
    assert response_event["duration_ms"] == 250.0
    assert response_event["response_content_type"] == "application/json"


def test_post_sse_decodes_utf8_across_chunks_and_multiline_events(
    monkeypatch, captured_model_events
):
    chunks = (
        b": keepalive\r\ndata: {\"delta\":\"\xe4\xbd",
        b"\xa0\"}\r\n\r\ndata: first\ndata: second\n\n",
        b"data: [DONE]\n\n",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept"] == "text/event-stream"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            stream=ChunkedAsyncStream(chunks),
        )

    install_transport(monkeypatch, handler)
    received = []
    ticks = iter([1.0, 1.25])

    status = asyncio.run(
        post_sse(
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            FAKE_API_KEY,
            {"model": "m", "stream": True},
            request_id=REQUEST_ID,
            provider="deepseek",
            model="m",
            on_data=received.append,
            clock=lambda: next(ticks),
        )
    )

    assert status == 200
    assert received == ['{"delta":"你"}', "first\nsecond", "[DONE]"]
    events = captured_model_events()
    assert [event["event"] for event in events] == [
        "llm_http_request",
        "llm_http_response",
    ]
    assert events[1]["duration_ms"] == 250.0


@pytest.mark.parametrize(
    "chunks",
    [
        (b"data: \xff\n\n",),
        (b'data: {"unfinished":true}',),
        (b"data: " + b"x" * (64 * 1024 + 1),),
    ],
)
def test_post_sse_rejects_invalid_or_unbounded_stream(monkeypatch, chunks):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkedAsyncStream(chunks),
        )

    install_transport(monkeypatch, handler, adapt_streaming_json=False)

    with pytest.raises(ProviderInvalidResponseError):
        asyncio.run(
            post_sse(
                DEEPSEEK_CHAT_COMPLETIONS_URL,
                FAKE_API_KEY,
                {"model": "m", "stream": True},
                request_id=REQUEST_ID,
                provider="deepseek",
                model="m",
                on_data=lambda data: None,
            )
        )


def test_post_sse_rejects_non_event_stream_content_type(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a stream"})

    install_transport(monkeypatch, handler, adapt_streaming_json=False)

    with pytest.raises(ProviderInvalidResponseError):
        asyncio.run(
            post_sse(
                DEEPSEEK_CHAT_COMPLETIONS_URL,
                FAKE_API_KEY,
                {"model": "m", "stream": True},
                request_id=REQUEST_ID,
                provider="deepseek",
                model="m",
                on_data=lambda data: None,
            )
        )


def test_post_sse_applies_overall_timeout_and_closes_stream(
    monkeypatch,
    captured_model_events,
):
    stream = BlockingAsyncStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    install_transport(monkeypatch, handler, adapt_streaming_json=False)
    monkeypatch.setattr(http_client, "TOTAL_TIMEOUT_SECONDS", 0.01)
    ticks = iter([3.0, 3.02])

    with pytest.raises(ProviderTimeoutError):
        asyncio.run(
            post_sse(
                DEEPSEEK_CHAT_COMPLETIONS_URL,
                FAKE_API_KEY,
                {"model": "m", "stream": True},
                request_id=REQUEST_ID,
                provider="deepseek",
                model="m",
                on_data=lambda data: None,
                clock=lambda: next(ticks),
            )
        )

    assert stream.closed is True
    events = captured_model_events()
    assert [event["event"] for event in events] == [
        "llm_http_request",
        "llm_http_error",
    ]
    assert events[1]["error_type"] == "timeout"


def test_post_sse_cancellation_closes_stream_without_network_error(
    monkeypatch,
    captured_model_events,
):
    async def scenario():
        stream = BlockingAsyncStream()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )

        install_transport(monkeypatch, handler, adapt_streaming_json=False)
        task = asyncio.create_task(
            post_sse(
                DEEPSEEK_CHAT_COMPLETIONS_URL,
                FAKE_API_KEY,
                {"model": "m", "stream": True},
                request_id=REQUEST_ID,
                provider="deepseek",
                model="m",
                on_data=lambda data: None,
            )
        )
        await stream.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed is True

    asyncio.run(scenario())
    assert [event["event"] for event in captured_model_events()] == [
        "llm_http_request"
    ]


@pytest.mark.parametrize("status_code", [401, 403, 500])
def test_post_json_logs_response_without_error_event_for_non_2xx(
    monkeypatch, captured_model_events, status_code
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "secret upstream body"})

    install_transport(monkeypatch, handler)
    ticks = iter([0.0, 1.5])

    with pytest.raises(LlmProviderError):
        asyncio.run(
            post_json(
                DEEPSEEK_CHAT_COMPLETIONS_URL,
                FAKE_API_KEY,
                {"model": "m", "messages": []},
                request_id=REQUEST_ID,
                provider="deepseek",
                model="m",
                clock=lambda: next(ticks),
            )
        )

    events = captured_model_events()
    assert [event["event"] for event in events] == [
        "llm_http_request",
        "llm_http_response",
    ]
    assert events[1]["status_code"] == status_code
    assert events[1]["duration_ms"] == 1500.0


def test_post_json_logs_timeout_error_with_controllable_clock(
    monkeypatch, captured_model_events
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret timeout detail", request=request)

    install_transport(monkeypatch, handler)
    ticks = iter([10.0, 10.5])

    with pytest.raises(ProviderTimeoutError):
        asyncio.run(
            post_json(
                DEEPSEEK_CHAT_COMPLETIONS_URL,
                FAKE_API_KEY,
                {"model": "m", "messages": []},
                request_id=REQUEST_ID,
                provider="deepseek",
                model="m",
                clock=lambda: next(ticks),
            )
        )

    events = captured_model_events()
    assert [event["event"] for event in events] == [
        "llm_http_request",
        "llm_http_error",
    ]
    error_event = events[1]
    assert error_event["error_type"] == "timeout"
    assert error_event["duration_ms"] == 500.0
    assert "secret" not in json.dumps(error_event, ensure_ascii=False)


def test_post_json_logs_connection_error_with_controllable_clock(
    monkeypatch, captured_model_events
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret connection detail", request=request)

    install_transport(monkeypatch, handler)
    ticks = iter([20.0, 20.75])

    with pytest.raises(ProviderConnectionError):
        asyncio.run(
            post_json(
                DEEPSEEK_CHAT_COMPLETIONS_URL,
                FAKE_API_KEY,
                {"model": "m", "messages": []},
                request_id=REQUEST_ID,
                provider="deepseek",
                model="m",
                clock=lambda: next(ticks),
            )
        )

    events = captured_model_events()
    assert [event["event"] for event in events] == [
        "llm_http_request",
        "llm_http_error",
    ]
    error_event = events[1]
    assert error_event["error_type"] == "connection"
    assert error_event["duration_ms"] == 750.0
    assert "secret" not in json.dumps(error_event, ensure_ascii=False)


def test_post_json_log_events_redact_authorization_and_keep_payload(
    monkeypatch, captured_model_events
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {FAKE_API_KEY}"
        return httpx.Response(200, json=_deepseek_ok_body())

    install_transport(monkeypatch, handler)

    asyncio.run(
        post_json(
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            FAKE_API_KEY,
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            request_id=REQUEST_ID,
            provider="deepseek",
            model="m",
        )
    )

    log_text = json.dumps(captured_model_events(), ensure_ascii=False)
    assert FAKE_API_KEY not in log_text
    assert "Bearer [REDACTED]" in log_text


@pytest.mark.parametrize(
    ("provider", "request_field"),
    [
        (AliyunResponsesProvider(FAKE_API_KEY, "qwen3-max"), "input"),
        (
            DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-flash"),
            "messages",
        ),
    ],
)
def test_system_prompt_is_recorded_in_complete_upstream_request_body(
    monkeypatch,
    captured_model_events,
    provider,
    request_field,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if provider.name == "aliyun":
            return httpx.Response(200, json={"output_text": "ok"})
        return httpx.Response(200, json=_deepseek_ok_body())

    install_transport(monkeypatch, handler)
    messages = (
        ChatMessage(ChatRole.SYSTEM, "logged project rules"),
        ChatMessage(ChatRole.USER, "question"),
    )

    asyncio.run(run_provider_once(provider, messages))

    request_event = next(
        event
        for event in captured_model_events()
        if event["event"] == "llm_http_request"
    )
    assert request_event["request_body"][request_field][0] == {
        "role": "system",
        "content": "logged project rules",
    }


def test_deepseek_logged_request_body_equals_actual_payload_for_single_and_multi_turn(
    monkeypatch, captured_model_events
):
    captured_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        return httpx.Response(200, json=_deepseek_ok_body())

    install_transport(monkeypatch, handler)
    provider = DeepSeekChatProvider(FAKE_API_KEY, "deepseek-v4-pro")

    asyncio.run(run_provider_once(provider, user_messages("第一问")))
    history = (
        ChatMessage(ChatRole.USER, "第一问"),
        ChatMessage(ChatRole.ASSISTANT, "第一答"),
        ChatMessage(ChatRole.USER, "第二问"),
    )
    asyncio.run(run_provider_once(provider, history))

    request_events = [
        event
        for event in captured_model_events()
        if event["event"] == "llm_http_request"
    ]
    assert len(request_events) == 2
    assert request_events[0]["request_body"] == captured_payloads[0]
    assert request_events[1]["request_body"] == captured_payloads[1]
    assert request_events[1]["request_body"]["messages"] == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
    ]
    for event in request_events:
        assert event["provider"] == "deepseek"
        assert event["model"] == "deepseek-v4-pro"
        assert event["request_id"] == REQUEST_ID


def test_aliyun_logged_request_body_equals_actual_payload_for_single_and_multi_turn(
    monkeypatch, captured_model_events
):
    captured_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"output_text": "ok"})

    install_transport(monkeypatch, handler)
    provider = AliyunResponsesProvider(FAKE_API_KEY, "custom-qwen")

    asyncio.run(run_provider_once(provider, user_messages("hello")))
    history = (
        ChatMessage(ChatRole.USER, "first"),
        ChatMessage(ChatRole.ASSISTANT, "answer"),
        ChatMessage(ChatRole.USER, "second"),
    )
    asyncio.run(run_provider_once(provider, history))

    request_events = [
        event
        for event in captured_model_events()
        if event["event"] == "llm_http_request"
    ]
    assert len(request_events) == 2
    assert request_events[0]["request_body"] == captured_payloads[0]
    # Turn 统一使用数组 input，便于后续追加 Function Call。
    assert request_events[0]["request_body"]["input"] == [
        {"role": "user", "content": "hello"}
    ]
    assert request_events[1]["request_body"] == captured_payloads[1]
    # 多轮 input 完整保留有序历史数组。
    assert request_events[1]["request_body"]["input"] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ]
    for event in request_events:
        assert event["provider"] == "aliyun"
        assert event["model"] == "custom-qwen"
        assert event["request_id"] == REQUEST_ID
