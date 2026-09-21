from datetime import datetime, timedelta, timezone

import pytest

from app.runtime.session_store import SessionStore
from app.services.llm.contracts import ChatMessage, ChatRole
from app.webui.sessions import (
    WebSessionCatalog,
    WebSessionNotFound,
    WebSessionStoreError,
)


class SequenceIds:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return f"{self.value:032x}"


class SequenceClock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return datetime(2026, 9, 18, 8, tzinfo=timezone.utc) + timedelta(
            seconds=self.value
        )


def create_catalog(tmp_path):
    return WebSessionCatalog(
        tmp_path / "web-sessions",
        legacy_path=tmp_path / "web-session.json",
        id_factory=SequenceIds(),
        now=SequenceClock(),
    )


def test_catalog_creates_selects_renames_and_sorts_sessions(tmp_path):
    catalog = create_catalog(tmp_path)
    first = catalog.current
    second = catalog.create()

    renamed = catalog.rename(second.id, "  方案讨论  ")
    selected = catalog.select(first.id)
    touched = catalog.touch(first.id, first_input="这是一个自动生成的会话标题，长度需要被安全限制")

    assert first.title == "新对话"
    assert renamed.title == "方案讨论"
    assert selected.id == first.id
    assert touched.title == "这是一个自动生成的会话标题，长度需要被安全限制"[:30]
    assert touched.auto_title is False
    assert catalog.current.id == first.id
    assert [item.id for item in catalog.list_records()] == [first.id, second.id]
    assert catalog.session_store(first.id).path.name == f"{first.id}.json"


def test_catalog_delete_current_falls_back_and_keeps_one_session(tmp_path):
    catalog = create_catalog(tmp_path)
    first = catalog.current
    second = catalog.create()
    second_path = catalog.session_store(second.id).path
    SessionStore(second_path).save(
        (
            ChatMessage(ChatRole.USER, "问题"),
            ChatMessage(ChatRole.ASSISTANT, "回答"),
        )
    )

    fallback = catalog.delete(second.id)
    replacement = catalog.delete(first.id)

    assert fallback.id == first.id
    assert replacement.id not in {first.id, second.id}
    assert catalog.current.id == replacement.id
    assert len(catalog.list_records()) == 1
    assert not second_path.exists()


def test_catalog_migrates_legacy_session_once(tmp_path):
    legacy = SessionStore(tmp_path / "web-session.json")
    legacy.save(
        (
            ChatMessage(ChatRole.USER, "旧问题"),
            ChatMessage(ChatRole.ASSISTANT, "旧回答"),
        )
    )
    ids = SequenceIds()
    clock = SequenceClock()

    first_load = WebSessionCatalog(
        tmp_path / "web-sessions",
        legacy_path=legacy.path,
        id_factory=ids,
        now=clock,
    )
    second_load = WebSessionCatalog(
        tmp_path / "web-sessions",
        legacy_path=legacy.path,
        id_factory=ids,
        now=clock,
    )

    assert first_load.current.title == "历史对话"
    assert second_load.current.id == first_load.current.id
    assert second_load.session_store(second_load.current.id).load() == legacy.load()
    assert len(second_load.list_records()) == 1


def test_catalog_delete_removes_private_migration_backup(tmp_path):
    catalog = create_catalog(tmp_path)
    target = catalog.current.id
    store = catalog.session_store(target)
    store.path.write_text('{"version":1,"messages":[]}', encoding="utf-8")
    store.save_state(store.load_state())
    assert store.backup_path.exists()

    catalog.delete(target)

    assert not store.path.exists()
    assert not store.backup_path.exists()


def test_catalog_rejects_invalid_ids_titles_and_corrupt_index(tmp_path):
    catalog = create_catalog(tmp_path)

    with pytest.raises(WebSessionNotFound):
        catalog.select("../web-session")
    with pytest.raises(ValueError):
        catalog.rename(catalog.current.id, "   ")
    with pytest.raises(ValueError):
        catalog.rename(catalog.current.id, "x" * 81)

    catalog.index_path.write_text('{"version":1}', encoding="utf-8")
    with pytest.raises(WebSessionStoreError):
        create_catalog(tmp_path)


def test_catalog_persists_private_atomic_index(tmp_path):
    catalog = create_catalog(tmp_path)
    catalog.create()

    reloaded = WebSessionCatalog(
        tmp_path / "web-sessions",
        legacy_path=tmp_path / "web-session.json",
    )

    assert reloaded.current.id == catalog.current.id
    assert len(reloaded.list_records()) == 2
    assert catalog.index_path.stat().st_mode & 0o777 == 0o600
    assert catalog.index_path.parent.stat().st_mode & 0o777 == 0o700


def test_catalog_limits_visible_sessions_and_keeps_current_available(tmp_path):
    catalog = create_catalog(tmp_path)
    first_id = catalog.current.id
    for _ in range(55):
        catalog.create()

    catalog.select(first_id)
    visible = catalog.list_records()

    assert len(visible) == 50
    assert visible[0].id == first_id
    assert len({item.id for item in visible}) == 50
