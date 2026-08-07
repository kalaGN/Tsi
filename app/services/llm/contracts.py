"""模型 Provider 的稳定契约和不泄漏上游细节的共享异常。"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderResult:
    """保留内部协议结果，并向 Runtime 提供统一文本。"""

    upstream_status: int
    raw_body: Any
    output_text: str


@dataclass(frozen=True)
class ProviderConfig:
    """环境解析结果；密钥不参与 repr，避免调试输出意外泄漏。"""

    provider: str
    model: str
    api_key: str = field(repr=False)

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key)


class LlmProvider(Protocol):
    """Runtime 所依赖的最小单轮模型能力。"""

    name: str
    model: str

    @property
    def api_key_configured(self) -> bool:
        ...

    async def generate(self, input_text: str) -> ProviderResult:
        ...


class LlmProviderError(Exception):
    """所有可安全传递到 Runtime 的 Provider 错误基类。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.user_message = message
        self.status_code = status_code


class ProviderConfigurationError(LlmProviderError):
    pass


class ProviderTimeoutError(LlmProviderError):
    def __init__(self) -> None:
        super().__init__("Upstream request timed out")


class ProviderConnectionError(LlmProviderError):
    def __init__(self) -> None:
        super().__init__("Unable to connect to upstream service")


class ProviderAuthenticationError(LlmProviderError):
    def __init__(self, status_code: int) -> None:
        super().__init__("Upstream authentication failed", status_code)


class ProviderResponseError(LlmProviderError):
    def __init__(self, status_code: int) -> None:
        super().__init__("Upstream service returned an error", status_code)


class ProviderInvalidResponseError(LlmProviderError):
    def __init__(
        self,
        message: str = "Upstream service returned invalid JSON",
    ) -> None:
        super().__init__(message)
