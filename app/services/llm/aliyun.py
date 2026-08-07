"""阿里云兼容模式 Responses API Provider。"""

from dataclasses import dataclass, field
from typing import Any, ClassVar

from app.services.llm.contracts import (
    ProviderConfigurationError,
    ProviderInvalidResponseError,
    ProviderResult,
)
from app.services.llm.http_client import post_json


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

    async def generate(self, input_text: str) -> ProviderResult:
        if not self.api_key_configured:
            raise ProviderConfigurationError("Upstream API key is not configured")

        status_code, body = await post_json(
            ALIYUN_RESPONSES_URL,
            self.api_key,
            {"model": self.model, "input": input_text},
        )
        return ProviderResult(
            upstream_status=status_code,
            raw_body=body,
            output_text=_extract_output_text(body),
        )


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


def _invalid_structure() -> ProviderInvalidResponseError:
    return ProviderInvalidResponseError(
        "Upstream service returned an invalid response"
    )
