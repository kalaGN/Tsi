"""阿里云兼容模式 Responses 流式 API Provider。"""

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar, Sequence

from app.services.llm.contracts import (
    ChatMessage,
    ModelStep,
    ProviderConfigurationError,
    ProviderInvalidResponseError,
    ProviderInvalidRequestError,
    TextDeltaHandler,
    validate_provider_messages,
)
from app.services.llm.http_client import (
    MAX_STREAM_OUTPUT_BYTES,
    MAX_STREAM_TOOL_ARGUMENT_BYTES,
    MAX_STREAM_TOOL_CALLS,
    post_sse,
)
from tools.contracts import ToolCall, ToolDefinition, ToolResult


ALIYUN_RESPONSES_URL = (
    "https://llm-h2k07hgnp4aylibi.cn-beijing.maas.aliyuncs.com/"
    "compatible-mode/v1/responses"
)
ALIYUN_DEFAULT_MODEL = "qwen3-max"


@dataclass(frozen=True)
class AliyunResponsesProvider:
    """把阿里云请求和多种 Responses 文本结构转换为统一结果。"""

    api_key: str = field(repr=False)
    model: str = ALIYUN_DEFAULT_MODEL
    name: ClassVar[str] = "aliyun"

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key.strip())

    def create_turn(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition],
        *,
        request_id: str,
    ) -> "AliyunTurn":
        if not self.api_key_configured:
            raise ProviderConfigurationError("Upstream API key is not configured")
        validated_messages = validate_provider_messages(messages)
        return AliyunTurn(
            api_key=self.api_key,
            model=self.model,
            input_items=[
                {"role": message.role.value, "content": message.content}
                for message in validated_messages
            ],
            tools=tuple(tools),
            request_id=request_id,
        )


class AliyunTurn:
    """维护 Responses input 项，并成对续接 Function Call 与结果。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        input_items: list[dict[str, Any]],
        tools: tuple[ToolDefinition, ...],
        request_id: str,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._input_items = input_items
        self._tools = tools
        self._request_id = request_id
        self._pending_calls: tuple[ToolCall, ...] = ()
        self._completed = False

    async def next(
        self,
        tool_results: Sequence[ToolResult] = (),
        *,
        on_text_delta: TextDeltaHandler | None = None,
    ) -> ModelStep:
        if self._completed:
            raise ProviderInvalidRequestError()
        self._append_call_result_pairs(tuple(tool_results))

        payload: dict[str, Any] = {
            "model": self._model,
            "input": self._input_items,
            "stream": True,
        }
        if self._tools:
            payload["tools"] = [_aliyun_tool(tool) for tool in self._tools]
            payload["tool_choice"] = "auto"

        stream_state = _AliyunStreamState(on_text_delta)
        status_code = await post_sse(
            ALIYUN_RESPONSES_URL,
            self._api_key,
            payload,
            request_id=self._request_id,
            provider=AliyunResponsesProvider.name,
            model=self._model,
            on_data=stream_state.accept,
        )
        calls, output_text = stream_state.finish()
        if calls:
            self._pending_calls = calls
            return ModelStep(status_code, output_text, calls)

        self._completed = True
        return ModelStep(status_code, output_text, ())

    def _append_call_result_pairs(
        self,
        results: tuple[ToolResult, ...],
    ) -> None:
        if not self._pending_calls:
            if results:
                raise ProviderInvalidRequestError()
            return
        if len(results) != len(self._pending_calls) or any(
            result.call_id != call.call_id
            for call, result in zip(self._pending_calls, results)
        ):
            raise ProviderInvalidRequestError()

        for call, result in zip(self._pending_calls, results):
            self._input_items.append(
                {
                    "type": "function_call",
                    "name": call.name,
                    "arguments": call.arguments_json,
                    "call_id": call.call_id,
                }
            )
            self._input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": result.call_id,
                    "output": result.output,
                }
            )
        self._pending_calls = ()


class _AliyunStreamState:
    """累积 Responses 流的最终文本、工具调用与 completed 完整对象。"""

    def __init__(self, on_text_delta: TextDeltaHandler | None) -> None:
        self._on_text_delta = on_text_delta
        self._text_fragments: list[str] = []
        self._text_bytes = 0
        self._done_text: str | None = None
        self._completed_response: dict[str, Any] | None = None
        self._completed_call_items: dict[str, dict[str, Any]] = {}
        self._argument_fragments: dict[str, list[str]] = {}
        self._argument_bytes: dict[str, int] = {}
        self._saw_done_marker = False

    def accept(self, data: str) -> None:
        """处理一个 Responses data 事件，并只发布最终回答文本 Delta。"""

        if self._completed_response is not None and data != "[DONE]":
            raise _invalid_structure()
        if data == "[DONE]":
            if self._saw_done_marker:
                raise _invalid_structure()
            self._saw_done_marker = True
            return
        try:
            event = json.loads(data)
        except (TypeError, ValueError) as exc:
            raise _invalid_structure() from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise _invalid_structure()

        event_type = event["type"]
        if event_type == "response.output_text.delta":
            self._append_text_delta(event.get("delta"))
        elif event_type == "response.output_text.done":
            text = event.get("text")
            if not isinstance(text, str) or not text or self._done_text is not None:
                raise _invalid_structure()
            self._done_text = text
        elif event_type == "response.output_item.done":
            self._accept_output_item(event.get("item"))
        elif event_type == "response.function_call_arguments.delta":
            self._append_argument_delta(event)
        elif event_type == "response.function_call_arguments.done":
            self._accept_argument_done(event)
        elif event_type == "response.completed":
            response = event.get("response")
            if not isinstance(response, dict) or self._completed_response is not None:
                raise _invalid_structure()
            self._completed_response = response
        elif event_type in {"response.failed", "response.incomplete"}:
            raise _invalid_structure()

    def finish(
        self,
    ) -> tuple[tuple[ToolCall, ...], str | None]:
        """以 completed 完整对象校验增量，并返回原有模型步骤信息。"""

        body = self._completed_response
        if not isinstance(body, dict) or body.get("status") != "completed":
            raise _invalid_structure()
        calls = _extract_function_calls(body)
        self._validate_completed_call_items(body, calls)

        streamed_text = "".join(self._text_fragments)
        if calls:
            output_text = _optional_output_text(body)
            if streamed_text and output_text is not None and streamed_text != output_text:
                raise _invalid_structure()
            if self._done_text is not None and streamed_text != self._done_text:
                raise _invalid_structure()
            return calls, streamed_text or output_text

        complete_text = _extract_output_text(body)
        if streamed_text and streamed_text != complete_text:
            raise _invalid_structure()
        if self._done_text is not None and self._done_text != complete_text:
            raise _invalid_structure()
        return (), streamed_text or complete_text

    def _append_text_delta(self, delta: Any) -> None:
        """追加非空文本并限制 UTF-8 累计大小。"""

        if not isinstance(delta, str) or not delta:
            raise _invalid_structure()
        self._text_bytes += len(delta.encode("utf-8"))
        if self._text_bytes > MAX_STREAM_OUTPUT_BYTES:
            raise _invalid_structure()
        self._text_fragments.append(delta)
        if self._on_text_delta is not None:
            self._on_text_delta(delta)

    def _accept_output_item(self, item: Any) -> None:
        """保存完整 function_call item，供 completed 对象交叉校验。"""

        if not isinstance(item, dict) or item.get("type") != "function_call":
            return
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id or item_id in self._completed_call_items:
            raise _invalid_structure()
        _extract_function_calls({"output": [item]})
        self._completed_call_items[item_id] = item
        if len(self._completed_call_items) > MAX_STREAM_TOOL_CALLS:
            raise _invalid_structure()

    def _append_argument_delta(self, event: dict[str, Any]) -> None:
        """按 item ID 拼接可选的 function argument Delta。"""

        item_id = event.get("item_id")
        delta = event.get("delta")
        if not isinstance(item_id, str) or not item_id or not isinstance(delta, str):
            raise _invalid_structure()
        fragments = self._argument_fragments.setdefault(item_id, [])
        if len(self._argument_fragments) > MAX_STREAM_TOOL_CALLS:
            raise _invalid_structure()
        fragments.append(delta)
        size = self._argument_bytes.get(item_id, 0) + len(delta.encode("utf-8"))
        if size > MAX_STREAM_TOOL_ARGUMENT_BYTES:
            raise _invalid_structure()
        self._argument_bytes[item_id] = size

    def _accept_argument_done(self, event: dict[str, Any]) -> None:
        """校验 arguments.done 完整值与此前 Delta 拼接一致。"""

        item_id = event.get("item_id")
        arguments = event.get("arguments")
        if not isinstance(item_id, str) or not item_id or not isinstance(arguments, str):
            raise _invalid_structure()
        if (
            item_id not in self._argument_fragments
            and len(self._argument_fragments) >= MAX_STREAM_TOOL_CALLS
        ):
            raise _invalid_structure()
        streamed = "".join(self._argument_fragments.get(item_id, ()))
        if streamed and streamed != arguments:
            raise _invalid_structure()
        if len(arguments.encode("utf-8")) > MAX_STREAM_TOOL_ARGUMENT_BYTES:
            raise _invalid_structure()
        self._argument_fragments[item_id] = [arguments]
        self._argument_bytes[item_id] = len(arguments.encode("utf-8"))

    def _validate_completed_call_items(
        self,
        body: dict[str, Any],
        calls: tuple[ToolCall, ...],
    ) -> None:
        """验证增量/Item 事件和 completed 中的完整工具参数一致。"""

        output = body.get("output")
        if not isinstance(output, list):
            if calls or self._completed_call_items or self._argument_fragments:
                raise _invalid_structure()
            return
        call_items = [
            item
            for item in output
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
        if len(call_items) != len(calls) or len(calls) > MAX_STREAM_TOOL_CALLS:
            raise _invalid_structure()
        by_id = {
            item.get("id"): item
            for item in call_items
            if isinstance(item.get("id"), str) and item.get("id")
        }
        if self._completed_call_items and self._completed_call_items != by_id:
            raise _invalid_structure()
        for item_id, fragments in self._argument_fragments.items():
            item = by_id.get(item_id)
            if item is None or item.get("arguments") != "".join(fragments):
                raise _invalid_structure()


def _extract_output_text(body: Any) -> str:
    """按兼容优先级提取阿里云 Responses API 的可展示文本。"""

    if not isinstance(body, dict):
        raise _invalid_structure()

    output_text = body.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    output = body.get("output")
    if not isinstance(output, list):
        raise _invalid_structure()

    direct_fragments = [
        item["text"]
        for item in output
        if isinstance(item, dict)
        and isinstance(item.get("text"), str)
        and item["text"]
    ]
    if direct_fragments:
        return "\n".join(direct_fragments)

    nested_fragments: list[str] = []
    for item in output:
        if not isinstance(item, dict) or not isinstance(item.get("content"), list):
            continue
        nested_fragments.extend(
            content_item["text"]
            for content_item in item["content"]
            if isinstance(content_item, dict)
            and isinstance(content_item.get("text"), str)
            and content_item["text"]
        )
    if nested_fragments:
        return "\n".join(nested_fragments)

    raise _invalid_structure()


def _extract_function_calls(body: Any) -> tuple[ToolCall, ...]:
    if not isinstance(body, dict):
        raise _invalid_structure()
    output = body.get("output")
    if output is None:
        return ()
    if not isinstance(output, list):
        raise _invalid_structure()

    calls: list[ToolCall] = []
    call_ids: set[str] = set()
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        call_id = item.get("call_id")
        name = item.get("name")
        arguments = item.get("arguments")
        if (
            not isinstance(call_id, str)
            or not call_id
            or call_id in call_ids
            or not isinstance(name, str)
            or not name
            or not isinstance(arguments, str)
        ):
            raise _invalid_structure()
        call_ids.add(call_id)
        calls.append(ToolCall(call_id, name, arguments))
    return tuple(calls)


def _optional_output_text(body: Any) -> str | None:
    output_text = body.get("output_text") if isinstance(body, dict) else None
    return output_text if isinstance(output_text, str) and output_text else None


def _aliyun_tool(definition: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "name": definition.name,
        "description": definition.description,
        "parameters": dict(definition.parameters),
    }


def _invalid_structure() -> ProviderInvalidResponseError:
    return ProviderInvalidResponseError(
        "Upstream service returned an invalid response"
    )
