"""DeepSeek 官方 Chat Completions API Provider。"""

from dataclasses import dataclass, field
from typing import Any, ClassVar

from app.services.llm.contracts import (
    ProviderConfigurationError,
    ProviderInvalidResponseError,
    ProviderResult,
)
from app.services.llm.http_client import post_json


DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"


@dataclass(frozen=True)
class DeepSeekChatProvider:
    """将单轮文本映射为 DeepSeek Chat Completions 请求。"""

    api_key: str = field(repr=False)
    model: str = DEEPSEEK_DEFAULT_MODEL
    name: ClassVar[str] = "deepseek"

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key.strip())

    async def generate(self, input_text: str) -> ProviderResult:
        if not self.api_key_configured:
            raise ProviderConfigurationError("Upstream API key is not configured")

        status_code, body = await post_json(
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            self.api_key,
            {
                "model": self.model,
                "messages": [{"role": "user", "content": input_text}],
                "stream": False,
            },
        )
        return ProviderResult(
            upstream_status=status_code,
            raw_body=body,
            output_text=_extract_output_text(body),
        )


def _extract_output_text(body: Any) -> str:
    """只接受官方非流式响应中的首个 assistant 文本。"""

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
    content = message.get("content")
    if not isinstance(content, str) or not content:
        raise _invalid_structure()
    return content


def _invalid_structure() -> ProviderInvalidResponseError:
    return ProviderInvalidResponseError(
        "Upstream service returned an invalid response"
    )
