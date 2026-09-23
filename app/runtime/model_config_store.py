"""Web/TUI 共用的私有模型配置；API Key 只写入本机受限文件。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.runtime.model_budget import strict_json
from app.services.llm.aliyun import ALIYUN_DEFAULT_MODEL
from app.services.llm.deepseek import DEEPSEEK_DEFAULT_MODEL
from app.services.llm.factory import MAX_MODELS_PER_PROVIDER, validate_model_name
from app.services.llm.contracts import ModelOption
from local_paths import data_root


DEFAULT_MODEL_CONFIG_PATH = data_root() / "model-config.json"
MAX_MODEL_CONFIG_BYTES = 16 * 1024
MAX_API_KEY_BYTES = 4096
DEFAULT_MODELS = {"deepseek": DEEPSEEK_DEFAULT_MODEL, "aliyun": ALIYUN_DEFAULT_MODEL}
MODEL_ENV_KEYS = frozenset({
    "LLM_PROVIDER", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_MODELS",
    "DASHSCOPE_API_KEY", "ALIYUN_MODEL", "ALIYUN_MODELS",
})


class ModelConfigError(Exception):
    """不包含密钥、路径或底层文件正文的安全错误。"""


class ModelConfigConflict(ModelConfigError):
    """另一页面先保存了模型配置。"""


class ModelConfigValidation(ModelConfigError):
    """请求数据无效，不触碰现有配置。"""


@dataclass(frozen=True, repr=False)
class ProviderSettings:
    models: tuple[str, ...]
    api_key: str


@dataclass(frozen=True, repr=False)
class ModelConfig:
    revision: int
    providers: dict[str, ProviderSettings]

    def environment(self) -> dict[str, str]:
        """适配现有 Provider 工厂，不读取进程环境中的模型字段。"""

        deepseek = self.providers["deepseek"]
        aliyun = self.providers["aliyun"]
        return {
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": deepseek.api_key,
            "DEEPSEEK_MODEL": deepseek.models[0],
            "DEEPSEEK_MODELS": ",".join(deepseek.models),
            "DASHSCOPE_API_KEY": aliyun.api_key,
            "ALIYUN_MODEL": aliyun.models[0],
            "ALIYUN_MODELS": ",".join(aliyun.models),
        }

    def public_payload(self) -> dict[str, object]:
        """设置页只知道密钥是否已配置，不拿到任何 Key 字符。"""

        return {
            "revision": self.revision,
            "providers": {
                name: {
                    "models": list(settings.models),
                    "api_key_configured": bool(settings.api_key),
                }
                for name, settings in self.providers.items()
            },
        }

    def options(self) -> tuple[ModelOption, ...]:
        """只暴露用户保存的候选，不追加工厂的历史默认模型。"""

        return tuple(
            ModelOption(name, model, bool(settings.api_key))
            for name, settings in self.providers.items()
            for model in settings.models
        )


def _defaults() -> ModelConfig:
    return ModelConfig(0, {
        name: ProviderSettings((model,), "") for name, model in DEFAULT_MODELS.items()
    })


def _validate_models(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_MODELS_PER_PROVIDER:
        raise ValueError("invalid models")
    models = tuple(validate_model_name(item) for item in value)
    if any(item is None for item in models) or len(set(models)) != len(models):
        raise ValueError("invalid models")
    return models


def _valid_key(value: object, *, allow_empty: bool) -> bool:
    return (
        isinstance(value, str)
        and (allow_empty or bool(value))
        and len(value.encode("utf-8")) <= MAX_API_KEY_BYTES
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _decode(value: object) -> ModelConfig:
    if not isinstance(value, dict) or set(value) != {"version", "revision", "providers"}:
        raise ValueError("invalid model config")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("invalid model config version")
    if type(value["revision"]) is not int or value["revision"] < 0:
        raise ValueError("invalid model config revision")
    providers = value["providers"]
    if not isinstance(providers, dict) or set(providers) != set(DEFAULT_MODELS):
        raise ValueError("invalid providers")
    decoded = {}
    for name in DEFAULT_MODELS:
        raw = providers[name]
        if not isinstance(raw, dict) or set(raw) != {"models", "api_key"}:
            raise ValueError("invalid provider settings")
        key = raw["api_key"]
        if not _valid_key(key, allow_empty=True):
            raise ValueError("invalid api key")
        decoded[name] = ProviderSettings(_validate_models(raw["models"]), key)
    return ModelConfig(value["revision"], decoded)


class ModelConfigStore:
    """严格读取、冲突检测和私有原子写入，不自动导入旧 `.env`。"""

    def __init__(self, path: Path = DEFAULT_MODEL_CONFIG_PATH):
        self.path = Path(path)

    @staticmethod
    def defaults() -> ModelConfig:
        """只用于首次启动或损坏配置的安全禁用模型回退。"""

        return _defaults()

    def load(self) -> ModelConfig:
        descriptor = None
        try:
            try:
                info = self.path.lstat()
            except FileNotFoundError:
                return _defaults()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("model config permissions")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > MAX_MODEL_CONFIG_BYTES:
                raise ValueError("model config file")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                raw = stream.read(MAX_MODEL_CONFIG_BYTES + 1)
            if len(raw) > MAX_MODEL_CONFIG_BYTES:
                raise ValueError("model config size")
            return _decode(strict_json(raw.decode("utf-8")))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ModelConfigError("模型配置无法读取，请检查本机配置文件及权限。") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def save(
        self, *, expected_revision: int, provider: str, models: list[str],
        api_key_action: str, api_key: str | None = None,
    ) -> ModelConfig:
        """只更新一个 Provider，Key 的保留/替换/删除必须显式声明。"""

        current = self.load()
        if type(expected_revision) is not int or expected_revision != current.revision:
            raise ModelConfigConflict("模型配置已更新，请重新加载后保存。")
        if (
            not isinstance(provider, str) or provider not in DEFAULT_MODELS
            or not isinstance(api_key_action, str) or api_key_action not in {"keep", "set", "clear"}
        ):
            raise ModelConfigValidation("模型配置字段无效。")
        try:
            validated_models = _validate_models(models)
            if api_key_action == "set":
                if not _valid_key(api_key, allow_empty=False):
                    raise ValueError("invalid api key")
                key = api_key
            elif api_key is not None:
                raise ValueError("unexpected api key")
            else:
                key = current.providers[provider].api_key if api_key_action == "keep" else ""
            providers = dict(current.providers)
            providers[provider] = ProviderSettings(validated_models, key)
            updated = ModelConfig(current.revision + 1, providers)
            encoded = (json.dumps({
                "version": 1, "revision": updated.revision,
                "providers": {
                    name: {"models": list(settings.models), "api_key": settings.api_key}
                    for name, settings in updated.providers.items()
                },
            }, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            if len(encoded) > MAX_MODEL_CONFIG_BYTES:
                raise ValueError("oversized")
        except (UnicodeError, ValueError, TypeError) as exc:
            raise ModelConfigValidation("模型配置无效，请检查模型名称和密钥长度。") from exc

        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.parent.is_symlink() or not self.path.parent.is_dir():
                raise OSError("invalid directory")
            os.chmod(self.path.parent, 0o700)
            descriptor, temporary_name = tempfile.mkstemp(dir=self.path.parent, prefix=".model-config-", suffix=".tmp")
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
                raise OSError("invalid target")
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise ModelConfigError("模型配置保存失败，原配置未被主动重置。") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return updated
