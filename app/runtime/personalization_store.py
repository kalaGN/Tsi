"""Web 个性化提示词的本机、版本化持久化。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from app.runtime.model_budget import strict_json


DEFAULT_PERSONALIZATION_PATH = Path(__file__).resolve().parents[2] / "data" / "personalization.json"
MAX_PERSONAL_PROMPT_BYTES = 16 * 1024
MAX_PERSONALIZATION_FILE_BYTES = 20 * 1024


class PersonalizationError(Exception):
    """不向 Web 页面暴露配置路径与原始文件内容。"""


class PersonalizationConflict(PersonalizationError):
    """另一页面已更新设置。"""


def _validate(value: object) -> dict[str, object]:
    """只接受固定结构，避免秘密字段和超长文本进入配置。"""

    if not isinstance(value, dict) or set(value) != {"version", "revision", "prompt"}:
        raise ValueError("个性化设置结构无效。")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("个性化设置版本无效。")
    if type(value["revision"]) is not int or value["revision"] < 0:
        raise ValueError("个性化设置修订号无效。")
    if not isinstance(value["prompt"], str) or len(value["prompt"].encode("utf-8")) > MAX_PERSONAL_PROMPT_BYTES:
        raise ValueError("自定义系统提示词不能超过 16 KiB。")
    return dict(value)


class PersonalizationStore:
    """保存一份 Web 全会话共享的提示词，失败时保留最近有效快照。"""

    def __init__(self, path: Path = DEFAULT_PERSONALIZATION_PATH):
        self.path = Path(path)
        self.current_prompt = ""

    def load(self) -> dict[str, object]:
        """读取磁盘配置；损坏时拒绝返回，不覆盖现有文件。"""

        try:
            if self.path.is_symlink():
                raise ValueError("symbolic link")
            if not self.path.exists():
                payload = {"version": 1, "revision": 0, "prompt": ""}
            else:
                with self.path.open("rb") as stream:
                    raw = stream.read(MAX_PERSONALIZATION_FILE_BYTES + 1)
                if len(raw) > MAX_PERSONALIZATION_FILE_BYTES:
                    raise ValueError("oversized")
                payload = _validate(strict_json(raw.decode("utf-8")))
        except (OSError, UnicodeError, ValueError) as exc:
            raise PersonalizationError("个性化设置无法读取，请检查本地配置文件。") from exc
        self.current_prompt = str(payload["prompt"])
        return dict(payload)

    def save(self, *, expected_revision: int, prompt: str) -> dict[str, object]:
        """校验修订号后原子替换；仅成功写入才发布新提示词。"""

        current = self.load()
        if type(expected_revision) is not int or expected_revision != current["revision"]:
            raise PersonalizationConflict("个性化设置已更新，请重新加载后保存。")
        payload = _validate({"version": 1, "revision": expected_revision + 1, "prompt": prompt})
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=".personalization-", suffix=".tmp")
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
            raise PersonalizationError("个性化设置保存失败，原配置未被主动重置。") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        self.current_prompt = str(payload["prompt"])
        return payload
