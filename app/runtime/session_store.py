"""v3 会话状态、只读迁移与隐私安全备份。"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Sequence

from app.runtime.memory import (
    MAX_PREFERENCES,
    ConversationState,
    UserPreference,
    is_safe_preference_content,
    parse_conversation_summary,
)
from app.runtime.model_budget import strict_json
from app.services.llm.contracts import ChatMessage, ChatRole


SESSION_SCHEMA_VERSION = 3
DEFAULT_SESSION_PATH = Path(__file__).resolve().parents[2] / "data" / "chat-session.json"


class SessionStoreError(Exception):
    """不携带本地路径、正文或底层异常的存储错误。"""


class SessionStore:
    """读取不改盘；旧格式只在第一次成功保存前创建原字节备份。"""

    def __init__(self, path: Path = DEFAULT_SESSION_PATH) -> None:
        self.path = Path(path)
        self.backup_path = self.path.with_name(self.path.name + ".pre-v3.bak")
        self._migration_source: bytes | None = None

    def load(self) -> tuple[ChatMessage, ...]:
        return self.load_state().messages

    def load_state(self) -> ConversationState:
        if not self.path.exists():
            self._migration_source = None
            return ConversationState()
        try:
            if self.path.is_symlink():
                raise ValueError("symbolic link")
            raw = self.path.read_bytes()
            payload = strict_json(raw.decode("utf-8"))
            state, version = _decode_payload(payload)
            self._migration_source = raw if version in {1, 2} else None
            return state
        except (OSError, UnicodeError, ValueError) as exc:
            raise SessionStoreError("Unable to load saved conversation") from exc

    def save(self, messages: Sequence[ChatMessage]) -> None:
        self.save_state(ConversationState(messages=tuple(messages)))

    def save_state(self, state: ConversationState, *, clear_backup: bool = False) -> None:
        try:
            validated = _validate_state(state)
            encoded = _encode_state(validated)
            self._discover_unloaded_migration()
            if clear_backup:
                self._remove_backup()
            elif self._migration_source is not None:
                self._prepare_migration_backup(self._migration_source)
            self._atomic_write(encoded)
            self._migration_source = None
            if clear_backup:
                self._remove_backup()
        except SessionStoreError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise SessionStoreError("Unable to save conversation") from exc

    def save_privacy_state(self, state: ConversationState) -> None:
        """清偏好等隐私操作不得在旧备份中留下被删除的数据。"""

        self.save_state(state, clear_backup=True)

    def clear(self) -> None:
        try:
            # 先清可能含有更旧正文的备份，再删除主文件，避免失败后仍留隐私副本。
            for target in (self.backup_path, self.path):
                if target.is_symlink():
                    raise OSError("symbolic link")
                target.unlink(missing_ok=True)
            self._migration_source = None
        except OSError as exc:
            raise SessionStoreError("Unable to clear saved conversation") from exc

    def _discover_unloaded_migration(self) -> None:
        if self._migration_source is not None or not self.path.exists():
            return
        if self.path.is_symlink():
            raise SessionStoreError("Unable to save conversation")
        raw = self.path.read_bytes()
        payload = strict_json(raw.decode("utf-8"))
        if isinstance(payload, dict) and payload.get("version") in {1, 2}:
            _decode_payload(payload)
            self._migration_source = raw

    def _prepare_migration_backup(self, original: bytes) -> None:
        if self.path.is_symlink() or not self.path.exists() or self.path.read_bytes() != original:
            raise SessionStoreError("Saved conversation changed during migration")
        if self.backup_path.is_symlink():
            raise SessionStoreError("Unable to create conversation migration backup")
        if self.backup_path.exists():
            if self.backup_path.read_bytes() != original:
                raise SessionStoreError("Conversation migration backup conflicts")
            return
        descriptor = None
        try:
            descriptor = os.open(self.backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise SessionStoreError("Unable to create conversation migration backup") from exc

    def _remove_backup(self) -> None:
        if self.backup_path.is_symlink():
            raise SessionStoreError("Unable to clear conversation migration backup")
        self.backup_path.unlink(missing_ok=True)

    def _atomic_write(self, encoded: bytes) -> None:
        temporary: Path | None = None
        descriptor: int | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
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
            if descriptor is not None:
                os.close(descriptor)
            raise SessionStoreError("Unable to save conversation") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _encode_state(state: ConversationState) -> bytes:
    payload = {
        "version": SESSION_SCHEMA_VERSION,
        "messages": [{"role": message.role.value, "content": message.content} for message in state.messages],
        "context": {
            "summary": state.summary.to_payload() if state.summary is not None else None,
            "summary_through_message_count": state.summary_through_message_count,
            "context_start_message_count": state.context_start_message_count,
        },
        "preferences": [
            {"id": item.id, "content": item.content, "source": item.source, "updated_at": item.updated_at}
            for item in state.preferences
        ],
    }
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _decode_payload(payload: object) -> tuple[ConversationState, int]:
    if not isinstance(payload, dict) or payload.get("version") not in {1, 2, 3}:
        raise ValueError("unsupported session schema")
    version = payload["version"]
    expected_fields = {"version", "messages"} if version == 1 else {"version", "messages", "context", "preferences"}
    if set(payload) != expected_fields:
        raise ValueError("session fields are invalid")
    messages = _decode_messages(payload.get("messages"))
    if version == 1:
        return _validate_state(ConversationState(messages=messages)), version
    context = payload.get("context")
    preferences_payload = payload.get("preferences")
    if not isinstance(context, dict) or not isinstance(preferences_payload, list):
        raise ValueError("memory fields are invalid")
    preferences = tuple(_decode_preference(item) for item in preferences_payload)
    if version == 2:
        if set(context) != {"summary", "summarized_message_count"}:
            raise ValueError("legacy context fields are invalid")
        if context["summary"] is not None and not isinstance(context["summary"], str):
            raise ValueError("legacy summary is invalid")
        boundary = context.get("summarized_message_count")
        if type(boundary) is not int or boundary < 0 or boundary > len(messages) or boundary % 2:
            raise ValueError("legacy context boundary is invalid")
        return _validate_state(ConversationState(messages, None, 0, boundary, preferences)), version
    if set(context) != {"summary", "summary_through_message_count", "context_start_message_count"}:
        raise ValueError("context fields are invalid")
    raw_summary = context.get("summary")
    summary = None if raw_summary is None else parse_conversation_summary(
        json.dumps(raw_summary, ensure_ascii=False, separators=(",", ":")),
        output_token_limit=10_000_000,
    )
    state = ConversationState(
        messages, summary,
        context.get("summary_through_message_count"),
        context.get("context_start_message_count"), preferences,
    )
    return _validate_state(state), version


def _decode_messages(payload: object) -> tuple[ChatMessage, ...]:
    if not isinstance(payload, list):
        raise ValueError("messages must be a list")
    messages = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise ValueError("message must be an object")
        try:
            role = ChatRole(item["role"])
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown message role") from exc
        messages.append(ChatMessage(role, item["content"]))
    return _validate_complete_history(messages)


def _decode_preference(payload: object) -> UserPreference:
    if not isinstance(payload, dict) or set(payload) != {"id", "content", "source", "updated_at"}:
        raise ValueError("preference must be an object")
    return UserPreference(payload["id"], payload["content"], payload["source"], payload["updated_at"])


def _validate_state(state: ConversationState) -> ConversationState:
    if not isinstance(state, ConversationState):
        raise ValueError("state is invalid")
    messages = _validate_complete_history(state.messages)
    count = len(messages)
    summary_through = state.summary_through_message_count
    context_start = state.context_start_message_count
    if any(type(value) is not int or value < 0 or value > count or value % 2 for value in (summary_through, context_start)):
        raise ValueError("context boundary is invalid")
    if summary_through > context_start:
        raise ValueError("context boundaries are out of order")
    if (state.summary is None and summary_through != 0) or (state.summary is not None and summary_through == 0):
        raise ValueError("summary coverage is invalid")
    if state.summary is not None:
        parse_conversation_summary(state.summary.to_json(), output_token_limit=10_000_000)
    if len(state.preferences) > MAX_PREFERENCES:
        raise ValueError("too many preferences")
    seen = set()
    for item in state.preferences:
        if (
            not isinstance(item, UserPreference) or not isinstance(item.id, str)
            or len(item.id) != 16 or any(character not in "0123456789abcdef" for character in item.id)
            or item.id in seen or not is_safe_preference_content(item.content)
            or item.source != "explicit" or not _is_utc_timestamp(item.updated_at)
        ):
            raise ValueError("preference is invalid")
        seen.add(item.id)
    return ConversationState(messages, state.summary, summary_through, context_start, state.preferences)


def _validate_complete_history(messages: Sequence[ChatMessage]) -> tuple[ChatMessage, ...]:
    normalized = tuple(messages)
    if len(normalized) % 2:
        raise ValueError("history must contain complete turns")
    for index, message in enumerate(normalized):
        expected = ChatRole.USER if index % 2 == 0 else ChatRole.ASSISTANT
        if not isinstance(message, ChatMessage) or message.role is not expected or not isinstance(message.content, str) or not message.content.strip():
            raise ValueError("history contains an invalid message")
    return normalized


def _is_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
