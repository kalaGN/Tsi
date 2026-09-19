"""Web 多会话索引、迁移与独立 SessionStore 定位。"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from app.runtime.session_store import SessionStore, SessionStoreError


INDEX_VERSION = 1
MAX_SESSIONS = 1_000
MAX_VISIBLE_SESSIONS = 50
MAX_TITLE_LENGTH = 80
DEFAULT_TITLE = "新对话"


class WebSessionStoreError(Exception):
    """不暴露磁盘路径或原始内容的 Web 会话存储错误。"""


class WebSessionNotFound(Exception):
    """请求的会话不存在或 ID 不合法。"""


@dataclass(frozen=True)
class WebSessionRecord:
    """索引中的单个会话元数据。"""

    id: str
    title: str
    created_at: str
    updated_at: str
    auto_title: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class WebSessionCatalog:
    """原子维护 Web 会话索引，并把内容委托给通用 SessionStore。"""

    def __init__(
        self,
        root: Path,
        *,
        legacy_path: Path | None = None,
        id_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root)
        self.index_path = self.root / "index.json"
        self.sessions_path = self.root / "sessions"
        self.legacy_path = Path(legacy_path) if legacy_path is not None else None
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._records: tuple[WebSessionRecord, ...] = ()
        self._current_id = ""
        self._load_or_initialize()

    @property
    def current(self) -> WebSessionRecord:
        return self._record(self._current_id)

    def list_records(self) -> tuple[WebSessionRecord, ...]:
        """返回按最近更新倒序排列的有限会话列表。"""

        ordered = sorted(
            self._records,
            key=lambda item: (item.updated_at, item.created_at, item.id),
            reverse=True,
        )
        visible = ordered[:MAX_VISIBLE_SESSIONS]
        if all(item.id != self._current_id for item in visible):
            visible = [self.current, *visible[: MAX_VISIBLE_SESSIONS - 1]]
        return tuple(visible)

    def session_store(self, session_id: str) -> SessionStore:
        """只为索引内的安全 ID 创建内容 Store。"""

        record = self._record(session_id)
        return SessionStore(self.sessions_path / f"{record.id}.json")

    def create(self) -> WebSessionRecord:
        """创建新元数据并原子切换当前会话。"""

        if len(self._records) >= MAX_SESSIONS:
            raise WebSessionStoreError("Web session limit reached")
        record = self._new_record(DEFAULT_TITLE, auto_title=True)
        self._save(self._records + (record,), record.id)
        return record

    def select(self, session_id: str) -> WebSessionRecord:
        """持久化当前会话选择。"""

        record = self._record(session_id)
        if record.id != self._current_id:
            self._save(self._records, record.id)
        return record

    def rename(self, session_id: str, title: str) -> WebSessionRecord:
        """校验并持久化手动标题，后续不再自动覆盖。"""

        normalized = _validate_title(title)
        record = replace(
            self._record(session_id),
            title=normalized,
            updated_at=self._timestamp(),
            auto_title=False,
        )
        self._replace(record)
        return record

    def touch(
        self,
        session_id: str,
        *,
        first_input: str | None = None,
    ) -> WebSessionRecord:
        """更新活跃时间，并在首轮成功时生成自动标题。"""

        record = self._record(session_id)
        title = record.title
        auto_title = record.auto_title
        if auto_title and first_input is not None:
            title = _automatic_title(first_input)
            auto_title = False
        updated = replace(
            record,
            title=title,
            updated_at=self._timestamp(),
            auto_title=auto_title,
        )
        self._replace(updated)
        return updated

    def delete(self, session_id: str) -> WebSessionRecord:
        """先提交安全索引，再尽力清理不再可达的内容文件。"""

        target = self._record(session_id)
        remaining = tuple(item for item in self._records if item.id != target.id)
        if not remaining:
            replacement = self._new_record(DEFAULT_TITLE, auto_title=True)
            remaining = (replacement,)
            current_id = replacement.id
        elif target.id == self._current_id:
            current_id = max(
                remaining,
                key=lambda item: (item.updated_at, item.created_at, item.id),
            ).id
        else:
            current_id = self._current_id
        self._save(remaining, current_id)
        try:
            (self.sessions_path / f"{target.id}.json").unlink(missing_ok=True)
        except OSError:
            # 索引已经安全提交，孤儿内容不会再次被加载。
            pass
        return self.current

    def _load_or_initialize(self) -> None:
        if self.index_path.exists():
            self._load_index()
            return
        if self.legacy_path is not None and self.legacy_path.exists():
            self._migrate_legacy()
            return
        record = self._new_record(DEFAULT_TITLE, auto_title=True)
        self._save((record,), record.id)

    def _load_index(self) -> None:
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            records, current_id = _decode_index(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise WebSessionStoreError("Unable to load web session index") from exc
        self._records = records
        self._current_id = current_id

    def _migrate_legacy(self) -> None:
        assert self.legacy_path is not None
        try:
            state = SessionStore(self.legacy_path).load_state()
            record = self._new_record("历史对话", auto_title=False)
            new_store = SessionStore(self.sessions_path / f"{record.id}.json")
            new_store.save_state(state)
            self._save((record,), record.id)
        except (SessionStoreError, OSError) as exc:
            raise WebSessionStoreError("Unable to migrate legacy web session") from exc

    def _replace(self, record: WebSessionRecord) -> None:
        records = tuple(record if item.id == record.id else item for item in self._records)
        self._save(records, self._current_id)

    def _save(
        self,
        records: tuple[WebSessionRecord, ...],
        current_id: str,
    ) -> None:
        payload = {
            "version": INDEX_VERSION,
            "current_session_id": current_id,
            "sessions": [
                {
                    **item.to_payload(),
                    "auto_title": item.auto_title,
                }
                for item in records
            ],
        }
        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.sessions_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
            os.chmod(self.sessions_path, 0o700)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.root,
                prefix=".index.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            os.chmod(temporary_path, 0o600)
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = None
            with handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.index_path)
        except (OSError, TypeError, ValueError) as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise WebSessionStoreError("Unable to save web session index") from exc
        self._records = records
        self._current_id = current_id

    def _record(self, session_id: str) -> WebSessionRecord:
        if not _is_session_id(session_id):
            raise WebSessionNotFound("Web session not found")
        record = next((item for item in self._records if item.id == session_id), None)
        if record is None:
            raise WebSessionNotFound("Web session not found")
        return record

    def _new_record(self, title: str, *, auto_title: bool) -> WebSessionRecord:
        existing = {item.id for item in self._records}
        for _ in range(10):
            session_id = self._id_factory()
            if _is_session_id(session_id) and session_id not in existing:
                timestamp = self._timestamp()
                return WebSessionRecord(
                    session_id,
                    _validate_title(title),
                    timestamp,
                    timestamp,
                    auto_title,
                )
        raise WebSessionStoreError("Unable to allocate web session id")

    def _timestamp(self) -> str:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise WebSessionStoreError("Web session clock must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_index(payload: object) -> tuple[tuple[WebSessionRecord, ...], str]:
    if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
        raise ValueError("unsupported web session index")
    raw_records = payload.get("sessions")
    current_id = payload.get("current_session_id")
    if (
        not isinstance(raw_records, list)
        or not raw_records
        or len(raw_records) > MAX_SESSIONS
        or not _is_session_id(current_id)
    ):
        raise ValueError("invalid web session index")
    records = tuple(_decode_record(item) for item in raw_records)
    ids = {item.id for item in records}
    if len(ids) != len(records) or current_id not in ids:
        raise ValueError("invalid web session references")
    return records, current_id


def _decode_record(payload: object) -> WebSessionRecord:
    if not isinstance(payload, dict):
        raise ValueError("invalid web session record")
    session_id = payload.get("id")
    title = payload.get("title")
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    auto_title = payload.get("auto_title")
    if (
        not _is_session_id(session_id)
        or type(auto_title) is not bool
        or not _is_timestamp(created_at)
        or not _is_timestamp(updated_at)
    ):
        raise ValueError("invalid web session record")
    return WebSessionRecord(
        session_id,
        _validate_title(title),
        created_at,
        updated_at,
        auto_title,
    )


def _validate_title(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("title must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_TITLE_LENGTH:
        raise ValueError("title length is invalid")
    return normalized


def _automatic_title(value: str) -> str:
    normalized = " ".join(value.split())
    return normalized[:30] or DEFAULT_TITLE


def _is_session_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
