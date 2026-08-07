"""大模型 Provider 公共入口。"""

from app.services.llm.contracts import LlmProvider, ProviderResult
from app.services.llm.factory import create_provider


__all__ = ["LlmProvider", "ProviderResult", "create_provider"]
