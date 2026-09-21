"""解析模型预算与压缩策略；页面覆盖和环境默认共用同一校验。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from app.services.llm.factory import validate_model_name


MODEL_DEFAULTS = {
    "context_window_tokens": 128_000,
    "max_output_tokens": 4096,
    "summary_output_tokens": 2048,
    "safety_margin_tokens": 4096,
}
COMPACTION_DEFAULTS = {
    "trigger_percent": 70,
    "target_percent": 50,
    "recent_turns": 6,
    "summary_timeout_seconds": 30,
    "summary_cooldown_seconds": 600,
}
FIELD_LIMITS = {
    **{key: (1, 10_000_000) for key in MODEL_DEFAULTS},
    "trigger_percent": (1, 99),
    "target_percent": (1, 98),
    "recent_turns": (0, 50),
    "summary_timeout_seconds": (1, 120),
    "summary_cooldown_seconds": (60, 3600),
}


def strict_json(text: str) -> object:
    """拒绝重复键和非有限数，避免不同解析器看到不同配置。"""

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("配置包含重复字段。")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("配置包含非法数字。")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except RecursionError as exc:
        raise ValueError("配置嵌套过深。") from exc


def validate_overrides(value: object, *, scope: str) -> dict[str, int]:
    if scope not in {"model", "compaction"}:
        raise ValueError("配置范围无效。")
    allowed = MODEL_DEFAULTS if scope == "model" else COMPACTION_DEFAULTS
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError("配置字段无效。")
    for key, number in value.items():
        low, high = FIELD_LIMITS[key]
        if type(number) is not int or not low <= number <= high:
            raise ValueError(f"{key} 必须是 {low} 到 {high} 之间的整数。")
        if key == "summary_cooldown_seconds" and number % 60:
            raise ValueError("失败冷却必须为整分钟。")
    return dict(value)


def model_identity(provider: object, model: object) -> tuple[str, str]:
    if provider not in ("deepseek", "aliyun") or validate_model_name(model) != model:
        raise ValueError("模型标识无效。")
    if not isinstance(model, str):
        raise ValueError("模型标识无效。")
    return str(provider), model


@dataclass(frozen=True)
class ModelBudget:
    context_window_tokens: int = 128_000
    max_output_tokens: int = 4096
    summary_output_tokens: int = 2048
    safety_margin_tokens: int = 4096
    trigger_percent: int = 70
    target_percent: int = 50
    recent_turns: int = 6
    summary_timeout_seconds: int = 30
    summary_cooldown_seconds: int = 600

    def __post_init__(self):
        validate_overrides({key: getattr(self, key) for key in MODEL_DEFAULTS}, scope="model")
        validate_overrides({key: getattr(self, key) for key in COMPACTION_DEFAULTS}, scope="compaction")
        if not 0 < self.target_tokens < self.trigger_tokens < self.input_limit:
            raise ValueError("预算必须满足：压缩目标 < 触发占比 < 可用输入上限。")
        if self.summary_input_limit <= 0:
            raise ValueError("上下文窗口不足以容纳摘要输出与安全余量。")

    @property
    def input_limit(self) -> int:
        return self.context_window_tokens - self.max_output_tokens - self.safety_margin_tokens

    @property
    def summary_input_limit(self) -> int:
        return self.context_window_tokens - self.summary_output_tokens - self.safety_margin_tokens

    @property
    def trigger_tokens(self) -> int:
        return self.input_limit * self.trigger_percent // 100

    @property
    def target_tokens(self) -> int:
        return self.input_limit * self.target_percent // 100


class ModelBudgetCatalog:
    """启动时捕获环境默认；调用时合并最新页面覆盖，不读取秘密变量。"""

    def __init__(self, environ: Mapping[str, str]):
        self._models: dict[tuple[str, str], dict[str, int]] = {}
        legacy = environ.get("TUI_CONTEXT_WINDOW_TOKENS")
        self._legacy: int | None = None
        if legacy is not None:
            try:
                self._legacy = int(legacy)
                ModelBudget(context_window_tokens=self._legacy)
            except (TypeError, ValueError) as exc:
                raise ValueError("TUI_CONTEXT_WINDOW_TOKENS 配置无效。") from exc
        raw = environ.get("LLM_MODEL_BUDGETS")
        if raw is None:
            return
        if len(raw.encode("utf-8")) > 32 * 1024:
            raise ValueError("模型预算配置过大。")
        entries = strict_json(raw)
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError("模型预算必须是 1 到 100 项的数组。")
        for entry in entries:
            if not isinstance(entry, dict) or "context_window_tokens" not in entry:
                raise ValueError("模型预算条目缺少窗口。")
            identity = model_identity(entry.get("provider"), entry.get("model"))
            if identity in self._models:
                raise ValueError("模型预算条目重复。")
            values = validate_overrides({k: v for k, v in entry.items() if k not in {"provider", "model"}}, scope="model")
            ModelBudget(**{**MODEL_DEFAULTS, **values})
            self._models[identity] = values

    def resolve(self, provider: str, model: str, settings: dict | None = None):
        identity = model_identity(provider, model)
        values = {**MODEL_DEFAULTS, **COMPACTION_DEFAULTS}
        sources = {key: "default" for key in values}
        if self._legacy is not None:
            values["context_window_tokens"] = self._legacy
            sources["context_window_tokens"] = "legacy_env"
        for key, value in self._models.get(identity, {}).items():
            values[key], sources[key] = value, "model_config"
        baseline = dict(values)
        snapshot = settings or {"compaction": {}, "models": []}
        overrides = dict(snapshot["compaction"])
        for entry in snapshot["models"]:
            if (entry["provider"], entry["model"]) == identity:
                overrides.update({k: v for k, v in entry.items() if k not in {"provider", "model"}})
        for key, value in overrides.items():
            values[key], sources[key] = value, "settings"
        return ModelBudget(**values), sources, baseline
