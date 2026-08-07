"""从环境配置选择模型 Provider，集中管理默认值和合法取值。"""

import os
from collections.abc import Mapping

from app.services.llm.aliyun import ALIYUN_DEFAULT_MODEL, AliyunResponsesProvider
from app.services.llm.contracts import (
    LlmProvider,
    ProviderConfig,
    ProviderConfigurationError,
)
from app.services.llm.deepseek import DEEPSEEK_DEFAULT_MODEL, DeepSeekChatProvider


def resolve_provider_config(
    environ: Mapping[str, str] | None = None,
) -> ProviderConfig:
    """解析环境配置；未指定 Provider 时默认使用 DeepSeek。"""

    values = os.environ if environ is None else environ
    raw_provider = values.get("LLM_PROVIDER")
    if raw_provider is None:
        provider = "deepseek"
    else:
        provider = raw_provider.strip().lower()
        if provider not in {"aliyun", "deepseek"}:
            raise ProviderConfigurationError(
                "Unsupported LLM provider configuration"
            )

    if provider == "aliyun":
        return ProviderConfig(
            provider=provider,
            model=_model_or_default(values.get("ALIYUN_MODEL"), ALIYUN_DEFAULT_MODEL),
            api_key=_normalized_secret(values.get("DASHSCOPE_API_KEY")),
        )

    return ProviderConfig(
        provider=provider,
        model=_model_or_default(
            values.get("DEEPSEEK_MODEL"),
            DEEPSEEK_DEFAULT_MODEL,
        ),
        api_key=_normalized_secret(values.get("DEEPSEEK_API_KEY")),
    )


def create_provider(environ: Mapping[str, str] | None = None) -> LlmProvider:
    """根据统一配置创建具体 Provider，不包含调用流程。"""

    config = resolve_provider_config(environ)
    if config.provider == "aliyun":
        return AliyunResponsesProvider(config.api_key, config.model)
    return DeepSeekChatProvider(config.api_key, config.model)


def _model_or_default(value: str | None, default: str) -> str:
    if value is None or not value.strip():
        return default
    return value.strip()


def _normalized_secret(value: str | None) -> str:
    return "" if value is None else value.strip()
