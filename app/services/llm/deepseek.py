"""DeepSeek 官方 Chat Completions API Provider。"""

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


DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"


@dataclass(frozen=True)
class DeepSeekChatProvider:
    """将中立消息映射为 DeepSeek Chat Completions 请求。"""

    api_key: str = field(repr=False)
    model: str = DEEPSEEK_DEFAULT_MODEL
    name: ClassVar[str] = "deepseek"

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key.strip())

    def create_turn(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition],
        *,
        request_id: str,
    ) -> "DeepSeekTurn":
        if not self.api_key_configured:
            raise ProviderConfigurationError("Upstream API key is not configured")
        validated_messages = validate_provider_messages(messages)
        return DeepSeekTurn(
            api_key=self.api_key,
            model=self.model,
            messages=[
                {"role": message.role.value, "content": message.content}
                for message in validated_messages
            ],
            tools=tuple(tools),
            request_id=request_id,
        )


class DeepSeekTurn:
    """保存 DeepSeek assistant/tool 消息，完成单个请求内的工具续接。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: tuple[ToolDefinition, ...],
        request_id: str,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._messages = messages
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
        self._append_tool_results(tuple(tool_results))

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": self._messages,
            "stream": False,
        }
        if self._tools:
            payload["tools"] = [_deepseek_tool(tool) for tool in self._tools]
            payload["tool_choice"] = "auto"

        status_code, body = await post_json(
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            self._api_key,
            payload,
            request_id=self._request_id,
            provider=DeepSeekChatProvider.name,
            model=self._model,
        )
        message = _extract_message(body)
        calls = _extract_tool_calls(message)
        if calls:
            self._messages.append(_assistant_tool_message(message, calls))
            self._pending_calls = calls
            intermediate_text = message.get("content")
            if not isinstance(intermediate_text, str) or not intermediate_text:
                intermediate_text = None
            return ModelStep(status_code, intermediate_text, calls)

        output_text = _extract_message_text(message)
        self._completed = True
        return ModelStep(status_code, output_text, ())

    def _append_tool_results(self, results: tuple[ToolResult, ...]) -> None:
        if not self._pending_calls:
            if results:
                raise ProviderInvalidRequestError()
            return
        if len(results) != len(self._pending_calls) or any(
            result.call_id != call.call_id
            for call, result in zip(self._pending_calls, results)
        ):
            raise ProviderInvalidRequestError()
        self._messages.extend(
            {
                "role": "tool",
                "tool_call_id": result.call_id,
                "content": result.output,
            }
            for result in results
        )
        self._pending_calls = ()


def _extract_message(body: Any) -> dict[str, Any]:
    """提取首个 DeepSeek assistant 消息供文本和工具解析共用。"""

    if not isinstance(body, dict):
        raise _invalid_structure()
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _invalid_structure()
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise _invalid_structure()
    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise _invalid_structure()
    return message


def _extract_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, str) or not content:
        raise _invalid_structure()
    return content


def _extract_tool_calls(message: dict[str, Any]) -> tuple[ToolCall, ...]:
    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        return ()
    if not isinstance(raw_calls, list):
        raise _invalid_structure()

    calls: list[ToolCall] = []
    call_ids: set[str] = set()
    for raw_call in raw_calls:
        if (
            not isinstance(raw_call, dict)
            or raw_call.get("type") != "function"
            or not isinstance(raw_call.get("function"), dict)
        ):
            raise _invalid_structure()
        call_id = raw_call.get("id")
        function = raw_call["function"]
        name = function.get("name")
        arguments = function.get("arguments")
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


def _assistant_tool_message(
    message: dict[str, Any],
    calls: tuple[ToolCall, ...],
) -> dict[str, Any]:
    rebuilt: dict[str, Any] = {
        "role": "assistant",
        "content": message.get("content"),
        "tool_calls": [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.arguments_json,
                },
            }
            for call in calls
        ],
    }
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str):
        rebuilt["reasoning_content"] = reasoning
    return rebuilt


def _deepseek_tool(definition: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": definition.name,
            "description": definition.description,
            "parameters": dict(definition.parameters),
        },
    }


def _invalid_structure() -> ProviderInvalidResponseError:
    return ProviderInvalidResponseError(
        "Upstream service returned an invalid response"
    )
