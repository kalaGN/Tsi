"""项目级上下文设置的有界版本化存储；不会修改环境或会话。"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path

from app.runtime.model_budget import model_identity, strict_json, validate_overrides


DEFAULT_CONTEXT_SETTINGS_PATH = Path(__file__).resolve().parents[2] / "data" / "context-settings.json"


class ContextSettingsError(Exception):
    """不向界面暴露原文件路径或配置正文。"""


class ContextSettingsConflict(ContextSettingsError):
    pass


def empty_settings() -> dict:
    return {"version": 1, "revision": 0, "compaction": {}, "models": []}


def validate_settings(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"version", "revision", "compaction", "models"}:
        raise ValueError("设置结构无效。")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("设置版本无效。")
    if type(value["revision"]) is not int or value["revision"] < 0:
        raise ValueError("设置修订号无效。")
    validate_overrides(value["compaction"], scope="compaction")
    models = value["models"]
    if not isinstance(models, list) or len(models) > 100:
        raise ValueError("模型设置数量无效。")
    seen = set()
    for entry in models:
        if not isinstance(entry, dict):
            raise ValueError("模型设置无效。")
        identity = model_identity(entry.get("provider"), entry.get("model"))
        if identity in seen:
            raise ValueError("模型设置重复。")
        seen.add(identity)
        validate_overrides({k: v for k, v in entry.items() if k not in {"provider", "model"}}, scope="model")
    return copy.deepcopy(value)


class ContextSettingsStore:
    """仅 Web 单进程写；revision 防止多个浏览器覆盖彼此的更新。"""

    def __init__(self, path: Path = DEFAULT_CONTEXT_SETTINGS_PATH):
        self.path = Path(path)
        self._last_good: dict | None = None
        self.warning: str | None = None

    def load(self, *, allow_stale: bool = False) -> dict:
        try:
            if self.path.is_symlink():
                raise ValueError("symbolic link")
            if not self.path.exists():
                payload = empty_settings()
            else:
                with self.path.open("rb") as stream:
                    raw = stream.read(64 * 1024 + 1)
                if len(raw) > 64 * 1024:
                    raise ValueError("oversized")
                payload = validate_settings(strict_json(raw.decode("utf-8")))
        except (OSError, UnicodeError, ValueError) as exc:
            self.warning = "上下文设置无法读取，正在使用上一有效配置。"
            if allow_stale and self._last_good is not None:
                return copy.deepcopy(self._last_good)
            raise ContextSettingsError("上下文设置无法读取，请检查本地配置文件。") from exc
        self._last_good = payload
        self.warning = None
        return copy.deepcopy(payload)

    def save(self, candidate: dict, *, expected_revision: int) -> dict:
        current = self.load()
        if type(expected_revision) is not int or current["revision"] != expected_revision:
            raise ContextSettingsConflict("设置已更新，请重新加载后保存。")
        payload = validate_settings({**candidate, "revision": expected_revision + 1})
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > 64 * 1024:
            raise ValueError("上下文设置过大。")
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=".context-settings-", suffix=".tmp")
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise ContextSettingsError("上下文设置保存失败，原配置未被主动重置。") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # 不用临时文件清理失败掩盖原保存异常。
        self._last_good = payload
        self.warning = None
        return copy.deepcopy(payload)
