"""当前 TUI 会话的版本化 JSON 持久化。"""

import json
import os
import tempfile
from pathlib import Path
from typing import Sequence

from app.services.llm.contracts import ChatMessage, ChatRole


SESSION_SCHEMA_VERSION = 1
DEFAULT_SESSION_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "chat-session.json"
)


class SessionStoreError(Exception):
    """不携带本地路径或底层异常的存储错误。"""


class SessionStore:
    """保存和恢复唯一当前会话。"""

    def __init__(self, path: Path = DEFAULT_SESSION_PATH) -> None:
        self.path = Path(path)

    def load(self) -> tuple[ChatMessage, ...]:
        if not self.path.exists():
            return ()

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return _decode_payload(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise SessionStoreError("Unable to load saved conversation") from exc

    def save(self, messages: Sequence[ChatMessage]) -> None:
        try:
            validated = _validate_complete_history(messages)
        except (TypeError, ValueError) as exc:
            raise SessionStoreError("Unable to save conversation") from exc
        payload = {
            "version": SESSION_SCHEMA_VERSION,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in validated
            ],
        }

        temporary_path: Path | None = None
        file_descriptor: int | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            file_descriptor, temporary_name = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            os.chmod(temporary_path, 0o600)
            handle = os.fdopen(file_descriptor, "w", encoding="utf-8")
            file_descriptor = None  # 文件对象从此负责关闭该描述符。
            with handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
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
            raise SessionStoreError("Unable to save conversation") from exc

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise SessionStoreError("Unable to clear saved conversation") from exc


def _decode_payload(payload: object) -> tuple[ChatMessage, ...]:
    if not isinstance(payload, dict) or payload.get("version") != SESSION_SCHEMA_VERSION:
        raise ValueError("unsupported session schema")
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("messages must be a list")

    messages: list[ChatMessage] = []
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            raise ValueError("message must be an object")
        try:
            role = ChatRole(raw_message.get("role"))
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown message role") from exc
        content = raw_message.get("content")
        if not isinstance(content, str):
            raise ValueError("message content must be text")
        messages.append(ChatMessage(role, content))
    return _validate_complete_history(messages)


def _validate_complete_history(
    messages: Sequence[ChatMessage],
) -> tuple[ChatMessage, ...]:
    """持久化历史只允许完整的 user/assistant 消息对。"""

    normalized = tuple(messages)
    if not normalized or len(normalized) % 2 != 0:
        raise ValueError("history must contain complete turns")
    for index, message in enumerate(normalized):
        expected_role = ChatRole.USER if index % 2 == 0 else ChatRole.ASSISTANT
        if (
            not isinstance(message, ChatMessage)
            or message.role is not expected_role
            or not isinstance(message.content, str)
            or not message.content.strip()
        ):
            raise ValueError("history contains an invalid message")
    return normalized
