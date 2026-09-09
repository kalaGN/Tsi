import pytest

from app.runtime.chat import ChatErrorCode, ChatRuntimeError, ChatRuntimeInfo
from app.runtime.model_selection import (
    MODEL_SELECTION_FALLBACK_WARNING,
    ModelSelectionError,
    ModelSelectionService,
)
from app.runtime.model_selection_store import (
    ModelSelection,
    ModelSelectionStoreError,
)
from app.services.llm.contracts import ModelOption, ProviderConfigurationError


class FakeProvider:
    def __init__(self, name, model, api_key_configured=True):
        self.name = name
        self.model = model
        self.api_key_configured = api_key_configured


class FakeStore:
    def __init__(self, selection=None):
        self.selection = selection
        self.saved = []

    def load(self):
        return self.selection

    def save(self, selection):
        self.saved.append(selection)


class FakeSession:
    def __init__(self, events):
        self.events = events
        self.provider = None

    def replace_provider(self, provider):
        self.events.append(("replace", provider.name, provider.model))
        self.provider = provider


OPTIONS = (
    ModelOption("deepseek", "deepseek-v4-flash", True),
    ModelOption("aliyun", "qwen3-max", True),
)


def test_restore_returns_matching_provider_from_fixed_options():
    calls = []
    store = FakeStore(ModelSelection("aliyun", "qwen3-max"))

    def factory(provider, model):
        calls.append((provider, model))
        return FakeProvider(provider, model)

    service = ModelSelectionService(OPTIONS, store, provider_factory=factory)

    result = service.restore()

    assert result.provider.model == "qwen3-max"
    assert result.warning is None
    assert calls == [("aliyun", "qwen3-max")]
    assert service.options is OPTIONS


@pytest.mark.parametrize(
    "selection",
    [
        ModelSelection("aliyun", "removed"),
        ModelSelection("deepseek", "missing-key"),
    ],
)
def test_restore_unavailable_selection_returns_safe_warning(selection):
    options = OPTIONS + (
        ModelOption("deepseek", "missing-key", False),
    )
    service = ModelSelectionService(options, FakeStore(selection))

    result = service.restore()

    assert result.provider is None
    assert result.warning == MODEL_SELECTION_FALLBACK_WARNING


def test_restore_store_or_provider_failure_returns_safe_warning():
    class FailingStore(FakeStore):
        def load(self):
            raise ModelSelectionStoreError("sensitive path")

    service = ModelSelectionService(OPTIONS, FailingStore())
    assert service.restore().warning == MODEL_SELECTION_FALLBACK_WARNING

    store = FakeStore(ModelSelection("aliyun", "qwen3-max"))

    def fail_factory(provider, model):
        raise ProviderConfigurationError("sensitive provider detail")

    service = ModelSelectionService(OPTIONS, store, provider_factory=fail_factory)
    assert service.restore().warning == MODEL_SELECTION_FALLBACK_WARNING


def test_switch_replaces_session_before_saving_and_returns_runtime_info():
    events = []

    class OrderedStore(FakeStore):
        def save(self, selection):
            events.append(("save", selection.provider, selection.model))
            super().save(selection)

    provider = FakeProvider("aliyun", "qwen3-max")
    service = ModelSelectionService(
        OPTIONS,
        OrderedStore(),
        provider_factory=lambda provider_name, model: provider,
    )

    result = service.switch(
        FakeSession(events),
        ModelOption("aliyun", "qwen3-max", True),
    )

    assert events == [
        ("replace", "aliyun", "qwen3-max"),
        ("save", "aliyun", "qwen3-max"),
    ]
    assert result.runtime_info == ChatRuntimeInfo("aliyun", "qwen3-max", True)
    assert result.warning is None


def test_switch_store_failure_keeps_replaced_provider_and_returns_warning():
    events = []

    class FailingStore(FakeStore):
        def save(self, selection):
            events.append(("save", selection.provider, selection.model))
            raise ModelSelectionStoreError("sensitive path")

    session = FakeSession(events)
    service = ModelSelectionService(
        OPTIONS,
        FailingStore(),
        provider_factory=lambda provider, model: FakeProvider(provider, model),
    )

    result = service.switch(
        session,
        ModelOption("aliyun", "qwen3-max", True),
    )

    assert session.provider.model == "qwen3-max"
    assert result.warning == "模型已切换，但无法保存启动选择。"


def test_switch_maps_expected_failures_to_stable_errors():
    missing_key = ModelOption("aliyun", "qwen3-max", False)
    service = ModelSelectionService(
        (missing_key,),
        FakeStore(),
        provider_factory=lambda provider, model: FakeProvider(provider, model),
    )
    with pytest.raises(ModelSelectionError) as exc_info:
        service.switch(FakeSession([]), missing_key)
    assert exc_info.value.user_message == "目标模型的 API Key 未配置。"

    def fail_factory(provider, model):
        raise ProviderConfigurationError("sensitive detail")

    service = ModelSelectionService(
        OPTIONS,
        FakeStore(),
        provider_factory=fail_factory,
    )
    with pytest.raises(ModelSelectionError) as exc_info:
        service.switch(
            FakeSession([]),
            ModelOption("aliyun", "qwen3-max", True),
        )
    assert exc_info.value.user_message == "模型切换失败。"
    assert "sensitive" not in str(exc_info.value)


def test_switch_preserves_safe_session_runtime_error():
    class BusySession(FakeSession):
        def replace_provider(self, provider):
            raise ChatRuntimeError(
                ChatErrorCode.CONFIGURATION,
                "Model cannot be changed while a request is active",
            )

    service = ModelSelectionService(
        OPTIONS,
        FakeStore(),
        provider_factory=lambda provider, model: FakeProvider(provider, model),
    )

    with pytest.raises(ModelSelectionError) as exc_info:
        service.switch(
            BusySession([]),
            ModelOption("aliyun", "qwen3-max", True),
        )

    assert (
        exc_info.value.user_message
        == "Model cannot be changed while a request is active"
    )
