"""DeepSeek 官方 Chat Completions 流式 API Provider。"""

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
    TokenUsage,
    validate_provider_messages,
)
from app.services.llm.http_client import (
    MAX_STREAM_OUTPUT_BYTES,
    MAX_STREAM_TOOL_ARGUMENT_BYTES,
    MAX_STREAM_TOOL_CALLS,
    post_sse,
)
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
        *,
        on_text_delta: TextDeltaHandler | None = None,
    ) -> ModelStep:
        if self._completed:
            raise ProviderInvalidRequestError()
        self._append_tool_results(tuple(tool_results))

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": self._messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self._tools:
            payload["tools"] = [_deepseek_tool(tool) for tool in self._tools]
            payload["tool_choice"] = "auto"

        stream_state = _DeepSeekStreamState(on_text_delta)
        status_code = await post_sse(
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            self._api_key,
            payload,
            request_id=self._request_id,
            provider=DeepSeekChatProvider.name,
            model=self._model,
            on_data=stream_state.accept,
        )
        message, calls, token_usage = stream_state.finish()
        if calls:
            self._messages.append(_assistant_tool_message(message, calls))
            self._pending_calls = calls
            intermediate_text = message.get("content")
            if not isinstance(intermediate_text, str) or not intermediate_text:
                intermediate_text = None
            return ModelStep(status_code, intermediate_text, calls, token_usage)

        output_text = _extract_message_text(message)
        self._completed = True
        return ModelStep(status_code, output_text, (), token_usage)

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


class _DeepSeekStreamState:
    """累积一个 Chat Completions 流，完成时生成既有 assistant 消息。"""

    _FINISH_REASONS = {
        "stop",
        "length",
        "content_filter",
        "tool_calls",
        "insufficient_system_resource",
    }

    def __init__(self, on_text_delta: TextDeltaHandler | None) -> None:
        self._on_text_delta = on_text_delta
        self._content: list[str] = []
        self._content_bytes = 0
        self._reasoning: list[str] = []
        self._reasoning_bytes = 0
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._token_usage: TokenUsage | None = None
        self._finish_reason: str | None = None
        self._done = False

    def accept(self, data: str) -> None:
        """校验一个 data 事件并累积文本、reasoning 与工具参数增量。"""

        if self._done:
            raise _invalid_structure()
        if data == "[DONE]":
            self._done = True
            return
        try:
            body = json.loads(data)
        except (TypeError, ValueError) as exc:
            raise _invalid_structure() from exc
        if not isinstance(body, dict):
            raise _invalid_structure()
        has_token_usage = body.get("usage") is not None
        if has_token_usage:
            self._accept_token_usage(body["usage"])
        choices = body.get("choices")
        if not isinstance(choices, list):
            raise _invalid_structure()
        if not choices:
            # include_usage 的最后一个 Chunk 没有 choices，但必须携带有效计量。
            if not has_token_usage:
                raise _invalid_structure()
            return
        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise _invalid_structure()
        choice = choices[0]
        if choice.get("index", 0) != 0:
            raise _invalid_structure()
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise _invalid_structure()
        self._append_optional_text(delta, "content", self._content, publish=True)
        self._append_optional_text(
            delta,
            "reasoning_content",
            self._reasoning,
            limit_reasoning=True,
        )
        self._append_tool_call_deltas(delta.get("tool_calls"))

        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            if (
                not isinstance(finish_reason, str)
                or finish_reason not in self._FINISH_REASONS
                or self._finish_reason is not None
            ):
                raise _invalid_structure()
            self._finish_reason = finish_reason

    def finish(
        self,
    ) -> tuple[dict[str, Any], tuple[ToolCall, ...], TokenUsage | None]:
        """要求正常结束，并把增量转换为 Provider 原有完整消息结构。"""

        if not self._done or self._finish_reason is None:
            raise _invalid_structure()
        content = "".join(self._content)
        calls = self._build_tool_calls()
        if calls and self._finish_reason != "tool_calls":
            raise _invalid_structure()
        if not calls and not content:
            raise _invalid_structure()

        message: dict[str, Any] = {
            "role": "assistant",
            "content": content or None,
        }
        reasoning = "".join(self._reasoning)
        if reasoning:
            message["reasoning_content"] = reasoning
        return message, calls, self._token_usage

    def _accept_token_usage(self, raw_usage: Any) -> None:
        """只接受一次完整 usage，并映射 DeepSeek 的字段命名。"""

        if self._token_usage is not None or not isinstance(raw_usage, dict):
            raise _invalid_structure()
        try:
            self._token_usage = TokenUsage(
                raw_usage["prompt_tokens"],
                raw_usage["completion_tokens"],
                raw_usage["total_tokens"],
            )
        except (KeyError, ValueError) as exc:
            raise _invalid_structure() from exc

    def _append_optional_text(
        self,
        delta: dict[str, Any],
        field_name: str,
        target: list[str],
        *,
        publish: bool = False,
        limit_reasoning: bool = False,
    ) -> None:
        """追加允许为 null/空串的文本字段，并限制最终输出字节数。"""

        value = delta.get(field_name)
        if value is None or value == "":
            return
        if not isinstance(value, str):
            raise _invalid_structure()
        if publish:
            self._content_bytes += len(value.encode("utf-8"))
            if self._content_bytes > MAX_STREAM_OUTPUT_BYTES:
                raise _invalid_structure()
        if limit_reasoning:
            self._reasoning_bytes += len(value.encode("utf-8"))
            if self._reasoning_bytes > MAX_STREAM_OUTPUT_BYTES:
                raise _invalid_structure()
        target.append(value)
        if publish and self._on_text_delta is not None:
            self._on_text_delta(value)

    def _append_tool_call_deltas(self, raw_calls: Any) -> None:
        """按官方 index 合并可能跨多个 SSE Chunk 的工具调用字段。"""

        if raw_calls is None:
            return
        if not isinstance(raw_calls, list):
            raise _invalid_structure()
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                raise _invalid_structure()
            index = raw_call.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise _invalid_structure()
            builder = self._tool_calls.setdefault(
                index,
                {"id": "", "type": "", "name": "", "arguments": ""},
            )
            if len(self._tool_calls) > MAX_STREAM_TOOL_CALLS:
                raise _invalid_structure()
            self._merge_once(builder, "id", raw_call.get("id"))
            self._merge_once(builder, "type", raw_call.get("type"))
            function = raw_call.get("function")
            if function is not None:
                if not isinstance(function, dict):
                    raise _invalid_structure()
                self._merge_once(builder, "name", function.get("name"))
                arguments = function.get("arguments")
                if arguments is not None:
                    if not isinstance(arguments, str):
                        raise _invalid_structure()
                    builder["arguments"] += arguments
                    if (
                        len(builder["arguments"].encode("utf-8"))
                        > MAX_STREAM_TOOL_ARGUMENT_BYTES
                    ):
                        raise _invalid_structure()

    @staticmethod
    def _merge_once(builder: dict[str, str], field_name: str, value: Any) -> None:
        """只接受一次非空标识字段，避免流中途篡改调用身份。"""

        if value is None:
            return
        if not isinstance(value, str) or not value or builder[field_name]:
            raise _invalid_structure()
        builder[field_name] = value

    def _build_tool_calls(self) -> tuple[ToolCall, ...]:
        """按连续 index 构造完整且 ID 唯一的中立工具调用。"""

        if not self._tool_calls:
            return ()
        indexes = sorted(self._tool_calls)
        if indexes != list(range(len(indexes))):
            raise _invalid_structure()
        calls: list[ToolCall] = []
        call_ids: set[str] = set()
        for index in indexes:
            builder = self._tool_calls[index]
            if (
                not builder["id"]
                or builder["id"] in call_ids
                or builder["type"] != "function"
                or not builder["name"]
                or not builder["arguments"]
            ):
                raise _invalid_structure()
            call_ids.add(builder["id"])
            calls.append(
                ToolCall(
                    builder["id"],
                    builder["name"],
                    builder["arguments"],
                )
            )
        return tuple(calls)


def _extract_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, str) or not content:
        raise _invalid_structure()
    return content


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
