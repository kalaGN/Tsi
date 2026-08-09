"""阿里云兼容模式 Responses API Provider。"""

from dataclasses import dataclass, field
from typing import Any, ClassVar, Sequence

from app.services.llm.contracts import (
    ChatMessage,
    ModelStep,
    ProviderConfigurationError,
    ProviderInvalidResponseError,
    ProviderInvalidRequestError,
    validate_provider_messages,
)
from app.services.llm.http_client import post_json
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
    ) -> ModelStep:
        if self._completed:
            raise ProviderInvalidRequestError()
        self._append_call_result_pairs(tuple(tool_results))

        payload: dict[str, Any] = {
            "model": self._model,
            "input": self._input_items,
        }
        if self._tools:
            payload["tools"] = [_aliyun_tool(tool) for tool in self._tools]
            payload["tool_choice"] = "auto"

        status_code, body = await post_json(
            ALIYUN_RESPONSES_URL,
            self._api_key,
            payload,
            request_id=self._request_id,
            provider=AliyunResponsesProvider.name,
            model=self._model,
        )
        calls = _extract_function_calls(body)
        if calls:
            self._pending_calls = calls
            return ModelStep(status_code, _optional_output_text(body), calls)

        output_text = _extract_output_text(body)
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
