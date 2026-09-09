"""当前 TUI 会话的版本化 JSON 持久化。"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Sequence

from app.runtime.memory import (
    MAX_PREFERENCES,
    MAX_SUMMARY_CHARS,
    ConversationState,
    UserPreference,
    is_safe_preference_content,
)
from app.services.llm.contracts import ChatMessage, ChatRole


SESSION_SCHEMA_VERSION = 2
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
        """保留旧调用方只读取完整 Transcript 的兼容入口。"""

        return self.load_state().messages

    def load_state(self) -> ConversationState:
        """读取 v1/v2 会话；v1 在内存中映射为空记忆状态。"""

        if not self.path.exists():
            return ConversationState()

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return _decode_payload(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise SessionStoreError("Unable to load saved conversation") from exc

    def save(self, messages: Sequence[ChatMessage]) -> None:
        """保留旧调用方保存纯历史的兼容入口，并写为 v2。"""

        self.save_state(ConversationState(messages=tuple(messages)))

    def save_state(self, state: ConversationState) -> None:
        """严格验证并原子保存完整 Transcript 与记忆状态。"""

        try:
            validated = _validate_state(state)
        except (TypeError, ValueError) as exc:
            raise SessionStoreError("Unable to save conversation") from exc
        payload = {
            "version": SESSION_SCHEMA_VERSION,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in validated.messages
            ],
            "context": {
                "summary": validated.summary,
                "summarized_message_count": validated.summarized_message_count,
            },
            "preferences": [
                {
                    "id": item.id,
                    "content": item.content,
                    "source": item.source,
                    "updated_at": item.updated_at,
                }
                for item in validated.preferences
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


def _decode_payload(payload: object) -> ConversationState:
    if not isinstance(payload, dict) or payload.get("version") not in {1, 2}:
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
    if payload["version"] == 1:
        return _validate_state(ConversationState(messages=tuple(messages)))

    raw_context = payload.get("context")
    raw_preferences = payload.get("preferences")
    if not isinstance(raw_context, dict) or not isinstance(raw_preferences, list):
        raise ValueError("memory fields are invalid")
    preferences = tuple(_decode_preference(item) for item in raw_preferences)
    return _validate_state(
        ConversationState(
            messages=tuple(messages),
            summary=raw_context.get("summary"),
            summarized_message_count=raw_context.get("summarized_message_count"),
            preferences=preferences,
        )
    )


def _decode_preference(payload: object) -> UserPreference:
    if not isinstance(payload, dict):
        raise ValueError("preference must be an object")
    return UserPreference(
        id=payload.get("id"),
        content=payload.get("content"),
        source=payload.get("source"),
        updated_at=payload.get("updated_at"),
    )


def _validate_state(state: ConversationState) -> ConversationState:
    if not isinstance(state, ConversationState):
        raise ValueError("state is invalid")
    messages = _validate_complete_history(state.messages)
    summary = state.summary
    if summary is not None and (
        not isinstance(summary, str)
        or not summary.strip()
        or len(summary) > MAX_SUMMARY_CHARS
    ):
        raise ValueError("summary is invalid")
    boundary = state.summarized_message_count
    if (
        type(boundary) is not int
        or boundary < 0
        or boundary > len(messages)
        or boundary % 2 != 0
    ):
        raise ValueError("summary boundary is invalid")
    if len(state.preferences) > MAX_PREFERENCES:
        raise ValueError("too many preferences")
    seen_ids: set[str] = set()
    for item in state.preferences:
        if (
            not isinstance(item, UserPreference)
            or not isinstance(item.id, str)
            or len(item.id) != 16
            or any(character not in "0123456789abcdef" for character in item.id)
            or item.id in seen_ids
            or not is_safe_preference_content(item.content)
            or item.source != "explicit"
            or not _is_utc_timestamp(item.updated_at)
        ):
            raise ValueError("preference is invalid")
        seen_ids.add(item.id)
    return ConversationState(messages, summary, boundary, state.preferences)


def _is_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0


def _validate_complete_history(
    messages: Sequence[ChatMessage],
) -> tuple[ChatMessage, ...]:
    """持久化历史只允许完整的 user/assistant 消息对。"""

    normalized = tuple(messages)
    if len(normalized) % 2 != 0:
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
