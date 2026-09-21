"""组合环境默认与页面覆盖，向交互入口提供不可变的请求配置。"""

from dataclasses import asdict, dataclass
from typing import Mapping, Sequence

from app.runtime.context_settings_store import ContextSettingsConflict, ContextSettingsError, ContextSettingsStore
from app.runtime.model_budget import COMPACTION_DEFAULTS, FIELD_LIMITS, MODEL_DEFAULTS, ModelBudget, ModelBudgetCatalog, validate_overrides


@dataclass(frozen=True)
class ContextSettingsSnapshot:
    budget: ModelBudget
    revision: int
    sources: Mapping[str, str]
    warning: str | None = None


class ContextSettings:
    """同步读写边界不让出事件循环；活跃请求由宿主负责互斥。"""

    def __init__(self, catalog: ModelBudgetCatalog, store: ContextSettingsStore):
        self.catalog = catalog
        self.store = store
        self._last_valid: dict | None = None

    def snapshot(self, provider: str, model: str) -> ContextSettingsSnapshot:
        values = self.store.load(allow_stale=True)
        warning = self.store.warning
        try:
            budget, sources, _ = self.catalog.resolve(provider, model, values)
        except ValueError as exc:
            if self._last_valid is None:
                raise ContextSettingsError("上下文预算配置无效，请检查窗口和压缩阈值。") from exc
            values = self._last_valid
            try:
                budget, sources, _ = self.catalog.resolve(provider, model, values)
            except ValueError as fallback_exc:
                raise ContextSettingsError("当前模型的上下文预算不可用。") from fallback_exc
            warning = "上下文设置无效，正在使用上一有效配置。"
        else:
            self._last_valid = values
        return ContextSettingsSnapshot(budget, values["revision"], sources, warning)

    def payload(self, provider: str, model: str) -> dict:
        snapshot = self.snapshot(provider, model)
        values = self._last_valid
        _, baseline_sources, baseline = self.catalog.resolve(provider, model)
        model_values = next((item for item in values["models"] if (item["provider"], item["model"]) == (provider, model)), {})
        return {
            "revision": snapshot.revision,
            "provider": provider, "model": model,
            "effective": asdict(snapshot.budget),
            "sources": dict(snapshot.sources),
            "baseline": baseline,
            "baseline_sources": baseline_sources,
            "overrides": {
                "model": {key: model_values[key] for key in MODEL_DEFAULTS if key in model_values},
                "compaction": dict(values["compaction"]),
            },
            "limits": {key: list(bounds) for key, bounds in FIELD_LIMITS.items()},
            "input_limit": snapshot.budget.input_limit,
            "summary_input_limit": snapshot.budget.summary_input_limit,
            "warning": snapshot.warning,
        }

    def save(
        self, *, expected_revision: int, scope: str, overrides: object,
        provider: str, model: str, configured_models: Sequence[tuple[str, str]],
    ) -> dict:
        if scope not in {"model", "compaction"}:
            raise ValueError("设置范围无效。")
        normalized = validate_overrides(overrides, scope=scope)
        values = self.store.load()
        if values["revision"] != expected_revision:
            raise ContextSettingsConflict("设置已更新，请重新加载后保存。")
        # 当前文件的组合若已损坏，不用一次局部写入悄悄覆盖故障。
        identities = set(configured_models) | {(provider, model)}
        try:
            for identity in identities:
                self.catalog.resolve(*identity, values)
        except ValueError as exc:
            raise ContextSettingsError("现有上下文设置无效，拒绝覆盖保存。") from exc
        if scope == "compaction":
            values["compaction"] = normalized
        else:
            values["models"] = [item for item in values["models"] if (item["provider"], item["model"]) != (provider, model)]
            if normalized:
                values["models"].append({"provider": provider, "model": model, **normalized})
        for identity in identities:
            self.catalog.resolve(*identity, values)
        saved = self.store.save(values, expected_revision=expected_revision)
        self._last_valid = saved
        return self.payload(provider, model)
