"""TUI 最近模型选择的独立、版本化 JSON 持久化。"""

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.services.llm.factory import validate_model_name


MODEL_SELECTION_SCHEMA_VERSION = 1
MAX_MODEL_SELECTION_BYTES = 4 * 1024
DEFAULT_MODEL_SELECTION_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "model-selection.json"
)
_SUPPORTED_PROVIDERS = frozenset({"deepseek", "aliyun"})
_PAYLOAD_KEYS = frozenset({"version", "provider", "model"})


@dataclass(frozen=True)
class ModelSelection:
    """不包含密钥的供应商与模型选择。"""

    provider: str
    model: str


class ModelSelectionStoreError(Exception):
    """不向 TUI 泄漏路径、文件正文或底层文件系统异常。"""


class ModelSelectionStore:
    """保存和恢复 TUI 最近一次成功切换的模型。"""

    def __init__(self, path: Path = DEFAULT_MODEL_SELECTION_PATH) -> None:
        self.path = Path(path)

    def load(self) -> ModelSelection | None:
        """读取严格的 v1 选择；文件不存在表示尚未保存偏好。"""

        file_descriptor: int | None = None
        try:
            try:
                path_stat = self.path.lstat()
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(path_stat.st_mode):
                raise ValueError("invalid model selection file")
            flags = os.O_RDONLY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            file_descriptor = os.open(self.path, flags)
            file_stat = os.fstat(file_descriptor)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_size > MAX_MODEL_SELECTION_BYTES
            ):
                raise ValueError("invalid model selection file")
            raw = _read_bounded(file_descriptor)
            if len(raw) > MAX_MODEL_SELECTION_BYTES:
                raise ValueError("model selection file is too large")
            payload = json.loads(raw.decode("utf-8"))
            return _decode_selection(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ModelSelectionStoreError(
                "Unable to load saved model selection"
            ) from exc
        finally:
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass

    def save(self, selection: ModelSelection) -> None:
        """严格验证并以私有权限原子保存最近模型选择。"""

        try:
            validated = _validate_selection(selection)
        except (TypeError, ValueError) as exc:
            raise ModelSelectionStoreError(
                "Unable to save model selection"
            ) from exc
        payload = {
            "version": MODEL_SELECTION_SCHEMA_VERSION,
            "provider": validated.provider,
            "model": validated.model,
        }

        temporary_path: Path | None = None
        file_descriptor: int | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            parent_stat = self.path.parent.lstat()
            if not stat.S_ISDIR(parent_stat.st_mode):
                raise OSError("model selection parent is not a directory")
            try:
                target_stat = self.path.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(target_stat.st_mode):
                    raise OSError("model selection target is not a regular file")

            file_descriptor, temporary_name = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            os.chmod(temporary_path, 0o600)
            handle = os.fdopen(file_descriptor, "w", encoding="utf-8")
            file_descriptor = None
            with handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            os.chmod(self.path, 0o600)
        except (OSError, TypeError, ValueError) as exc:
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ModelSelectionStoreError(
                "Unable to save model selection"
            ) from exc


def _decode_selection(payload: object) -> ModelSelection:
    if (
        not isinstance(payload, dict)
        or frozenset(payload) != _PAYLOAD_KEYS
        or type(payload.get("version")) is not int
        or payload.get("version") != MODEL_SELECTION_SCHEMA_VERSION
    ):
        raise ValueError("unsupported model selection schema")
    return _validate_selection(
        ModelSelection(
            provider=payload.get("provider"),
            model=payload.get("model"),
        )
    )


def _read_bounded(file_descriptor: int) -> bytes:
    """完整读取小文件，同时用额外一字节识别读取期间的增长。"""

    remaining = MAX_MODEL_SELECTION_BYTES + 1
    chunks: list[bytes] = []
    while remaining:
        chunk = os.read(file_descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _validate_selection(selection: ModelSelection) -> ModelSelection:
    if not isinstance(selection, ModelSelection):
        raise ValueError("model selection is invalid")
    provider = selection.provider
    if not isinstance(provider, str) or provider not in _SUPPORTED_PROVIDERS:
        raise ValueError("model provider is invalid")
    model = validate_model_name(selection.model)
    if model is None:
        raise ValueError("model name is invalid")
    return ModelSelection(provider, model)
