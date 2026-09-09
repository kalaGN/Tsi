"""恢复和切换 TUI 模型的 Provider 中立用例。"""

from collections.abc import Callable
from dataclasses import dataclass

from app.runtime.chat import ChatRuntimeError, ChatRuntimeInfo
from app.runtime.model_selection_store import (
    ModelSelection,
    ModelSelectionStore,
    ModelSelectionStoreError,
)
from app.runtime.session import ChatSession
from app.services.llm.contracts import (
    LlmProvider,
    LlmProviderError,
    ModelOption,
)
from app.services.llm.factory import create_provider_for_model


MODEL_SELECTION_FALLBACK_WARNING = (
    "已忽略无法恢复的模型选择，当前使用环境默认模型。"
)
MODEL_SELECTION_SAVE_WARNING = "模型已切换，但无法保存启动选择。"

ProviderFactory = Callable[[str, str], LlmProvider]


@dataclass(frozen=True)
class ModelRestoreResult:
    """启动恢复得到的 Provider 或安全降级提示。"""

    provider: LlmProvider | None
    warning: str | None = None


@dataclass(frozen=True)
class ModelSwitchResult:
    """成功切换后的安全运行快照与可选持久化提示。"""

    runtime_info: ChatRuntimeInfo
    warning: str | None = None


class ModelSelectionError(Exception):
    """TUI 可直接展示的稳定模型选择错误。"""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class ModelSelectionService:
    """封装候选快照、Provider 创建、Session 替换和选择保存。"""

    def __init__(
        self,
        options: tuple[ModelOption, ...],
        store: ModelSelectionStore | None = None,
        *,
        provider_factory: ProviderFactory = create_provider_for_model,
    ) -> None:
        self._options = tuple(options)
        self._store = store
        self._provider_factory = provider_factory

    @property
    def options(self) -> tuple[ModelOption, ...]:
        return self._options

    def restore(self) -> ModelRestoreResult:
        """仅恢复仍在候选快照中且密钥可用的保存项。"""

        if self._store is None:
            return ModelRestoreResult(None)
        try:
            selection = self._store.load()
        except ModelSelectionStoreError:
            return ModelRestoreResult(None, MODEL_SELECTION_FALLBACK_WARNING)
        if selection is None:
            return ModelRestoreResult(None)
        option = self._find_option(selection.provider, selection.model)
        if option is None or not option.api_key_configured:
            return ModelRestoreResult(None, MODEL_SELECTION_FALLBACK_WARNING)
        try:
            provider = self._provider_factory(option.provider, option.model)
        except (LlmProviderError, ValueError):
            return ModelRestoreResult(None, MODEL_SELECTION_FALLBACK_WARNING)
        if not provider.api_key_configured:
            return ModelRestoreResult(None, MODEL_SELECTION_FALLBACK_WARNING)
        return ModelRestoreResult(provider)

    def switch(
        self,
        session: ChatSession,
        selection: ModelOption,
    ) -> ModelSwitchResult:
        """创建并替换 Provider，成功后再持久化安全选择。"""

        option = self._find_option(selection.provider, selection.model)
        if option is None:
            raise ModelSelectionError("模型切换失败。")
        if not option.api_key_configured:
            raise ModelSelectionError("目标模型的 API Key 未配置。")
        try:
            provider = self._provider_factory(option.provider, option.model)
            if not provider.api_key_configured:
                raise ModelSelectionError("目标模型的 API Key 未配置。")
            session.replace_provider(provider)
        except ModelSelectionError:
            raise
        except ChatRuntimeError as exc:
            raise ModelSelectionError(exc.user_message) from exc
        except (LlmProviderError, ValueError) as exc:
            raise ModelSelectionError("模型切换失败。") from exc

        warning = None
        if self._store is not None:
            try:
                self._store.save(ModelSelection(provider.name, provider.model))
            except ModelSelectionStoreError:
                warning = MODEL_SELECTION_SAVE_WARNING
        return ModelSwitchResult(
            ChatRuntimeInfo(
                provider=provider.name,
                model=provider.model,
                api_key_configured=provider.api_key_configured,
            ),
            warning,
        )

    def _find_option(self, provider: str, model: str) -> ModelOption | None:
        return next(
            (
                option
                for option in self._options
                if (option.provider, option.model) == (provider, model)
            ),
            None,
        )
