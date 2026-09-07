"""从环境配置选择模型 Provider，集中管理默认值和合法取值。"""

import os
from collections.abc import Mapping

from app.services.llm.aliyun import ALIYUN_DEFAULT_MODEL, AliyunResponsesProvider
from app.services.llm.contracts import (
    LlmProvider,
    ModelOption,
    ProviderConfig,
    ProviderConfigurationError,
)
from app.services.llm.deepseek import DEEPSEEK_DEFAULT_MODEL, DeepSeekChatProvider


MAX_MODELS_PER_PROVIDER = 50
MAX_MODEL_LIST_CHARACTERS = 8 * 1024


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


def resolve_model_options(
    environ: Mapping[str, str] | None = None,
) -> tuple[ModelOption, ...]:
    """从有界环境配置构造不含密钥正文的 TUI 模型候选。"""

    values = os.environ if environ is None else environ
    options: list[ModelOption] = []
    provider_specs = (
        (
            "deepseek",
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_MODEL",
            "DEEPSEEK_MODELS",
            DEEPSEEK_DEFAULT_MODEL,
        ),
        (
            "aliyun",
            "DASHSCOPE_API_KEY",
            "ALIYUN_MODEL",
            "ALIYUN_MODELS",
            ALIYUN_DEFAULT_MODEL,
        ),
    )
    for provider, key_name, model_name, models_name, default_model in provider_specs:
        models = _model_candidates(
            values.get(models_name),
            values.get(model_name),
            default_model,
        )
        key_configured = bool(_normalized_secret(values.get(key_name)))
        options.extend(
            ModelOption(provider, model, key_configured) for model in models
        )
    return tuple(options)


def create_provider_for_model(
    provider: str,
    model: str,
    environ: Mapping[str, str] | None = None,
) -> LlmProvider:
    """为 TUI 明确选择创建 Provider，不修改部署环境或 HTTP 默认选择。"""

    values = os.environ if environ is None else environ
    normalized_provider = provider.strip().lower() if isinstance(provider, str) else ""
    normalized_model = _validated_model_name(model)
    if normalized_provider not in {"aliyun", "deepseek"}:
        raise ProviderConfigurationError("Unsupported LLM provider configuration")
    if normalized_model is None:
        raise ProviderConfigurationError("Unsupported LLM model configuration")
    if normalized_provider == "aliyun":
        return AliyunResponsesProvider(
            _normalized_secret(values.get("DASHSCOPE_API_KEY")),
            normalized_model,
        )
    return DeepSeekChatProvider(
        _normalized_secret(values.get("DEEPSEEK_API_KEY")),
        normalized_model,
    )


def _model_candidates(
    configured_list: str | None,
    configured_model: str | None,
    default_model: str,
) -> tuple[str, ...]:
    """优先保留目录顺序，并为当前值和默认值预留候选位置。"""

    required: list[str] = []
    for value in (_model_or_default(configured_model, default_model), default_model):
        normalized = _validated_model_name(value)
        if normalized is not None and normalized not in required:
            required.append(normalized)

    candidates: list[str] = []
    available_slots = MAX_MODELS_PER_PROVIDER - len(required)
    if (
        isinstance(configured_list, str)
        and len(configured_list) <= MAX_MODEL_LIST_CHARACTERS
    ):
        for value in configured_list.split(","):
            normalized = _validated_model_name(value)
            if normalized is None or normalized in candidates:
                continue
            if len(candidates) >= available_slots:
                break
            candidates.append(normalized)
    for value in required:
        if value not in candidates:
            candidates.append(value)
    return tuple(candidates)


def _model_or_default(value: str | None, default: str) -> str:
    if value is None or not value.strip():
        return default
    return value.strip()


def _validated_model_name(value: object) -> str | None:
    """拒绝可能污染终端或破坏列表格式的模型名称。"""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 128
        or "," in normalized
        or any(not character.isprintable() for character in normalized)
    ):
        return None
    return normalized


def _normalized_secret(value: str | None) -> str:
    return "" if value is None else value.strip()
