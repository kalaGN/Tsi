import json
import os
import stat

import pytest

from app.runtime import session_store
from app.runtime.memory import ConversationState, ConversationSummary, UserPreference
from app.runtime.session_store import SessionStore, SessionStoreError
from app.services.llm.contracts import ChatMessage, ChatRole


HISTORY = (ChatMessage(ChatRole.USER, "你好"), ChatMessage(ChatRole.ASSISTANT, "你好！"))
SUMMARY = ConversationSummary("继续开发", ("使用 v3",), (), (), ("测试",), (), ())
PREFERENCE = UserPreference("0" * 16, "使用中文回复", "explicit", "2026-09-09T00:00:00Z")


def test_missing_session_file_loads_empty_history(tmp_path):
    assert SessionStore(tmp_path / "nested" / "chat-session.json").load_state() == ConversationState()


def test_v3_round_trip_keeps_full_history_summary_boundaries_and_private_mode(tmp_path):
    path = tmp_path / "data" / "chat-session.json"
    state = ConversationState(HISTORY, SUMMARY, 2, 2, (PREFERENCE,))
    store = SessionStore(path)
    store.save_state(state)
    assert store.load_state() == state
    assert json.loads(path.read_text())["version"] == 3
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_v1_read_is_side_effect_free_then_first_save_creates_exact_backup(tmp_path):
    path = tmp_path / "chat-session.json"
    original = json.dumps({"version": 1, "messages": [{"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "旧回答"}]}, ensure_ascii=False).encode()
    path.write_bytes(original)
    store = SessionStore(path)
    state = store.load_state()
    assert state == ConversationState(messages=(ChatMessage(ChatRole.USER, "旧问题"), ChatMessage(ChatRole.ASSISTANT, "旧回答")))
    assert not store.backup_path.exists()
    store.save_state(ConversationState(state.messages, SUMMARY, 2, 2, (PREFERENCE,)))
    assert store.backup_path.read_bytes() == original
    assert stat.S_IMODE(store.backup_path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["version"] == 3


def test_v2_invalidates_free_text_summary_but_preserves_gap_preferences_and_history(tmp_path):
    path = tmp_path / "chat-session.json"
    payload = {
        "version": 2,
        "messages": [{"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "旧回答"}],
        "context": {"summary": "无法证明覆盖范围", "summarized_message_count": 2},
        "preferences": [PREFERENCE.__dict__],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    state = SessionStore(path).load_state()
    assert state.messages == HISTORY.__class__((ChatMessage(ChatRole.USER, "旧问题"), ChatMessage(ChatRole.ASSISTANT, "旧回答")))
    assert state.summary is None
    assert state.summary_through_message_count == 0
    assert state.context_start_message_count == 2
    assert state.preferences == (PREFERENCE,)


def test_migration_rejects_source_changed_since_read(tmp_path):
    path = tmp_path / "chat-session.json"
    path.write_text(json.dumps({"version": 1, "messages": []}), encoding="utf-8")
    store = SessionStore(path)
    store.load_state()
    path.write_text(json.dumps({"version": 1, "messages": [
        {"role": "user", "content": "变化"}, {"role": "assistant", "content": "回答"},
    ]}), encoding="utf-8")
    with pytest.raises(SessionStoreError, match="changed"):
        store.save_state(ConversationState())
    assert not store.backup_path.exists()


def test_existing_identical_backup_is_reused_but_conflict_and_symlink_are_rejected(tmp_path):
    path = tmp_path / "chat-session.json"
    raw = json.dumps({"version": 1, "messages": []}).encode()
    path.write_bytes(raw)
    store = SessionStore(path)
    store.load_state()
    store.backup_path.write_bytes(raw)
    store.save_state(ConversationState())
    assert store.backup_path.read_bytes() == raw

    path.write_bytes(raw)
    conflict = SessionStore(path)
    conflict.load_state()
    conflict.backup_path.write_text("different")
    with pytest.raises(SessionStoreError, match="conflicts"):
        conflict.save_state(ConversationState())
    conflict.backup_path.unlink()
    conflict.backup_path.symlink_to(path)
    with pytest.raises(SessionStoreError, match="backup"):
        conflict.save_state(ConversationState())


def test_privacy_save_and_clear_remove_migration_backup(tmp_path):
    path = tmp_path / "chat-session.json"
    path.write_text(json.dumps({"version": 2, "messages": [], "context": {"summary": None, "summarized_message_count": 0}, "preferences": [PREFERENCE.__dict__]}), encoding="utf-8")
    store = SessionStore(path)
    state = store.load_state()
    store.save_state(state)
    assert store.backup_path.exists()
    store.save_privacy_state(ConversationState())
    assert not store.backup_path.exists()
    store.clear()
    assert not path.exists() and not store.backup_path.exists()


@pytest.mark.parametrize("state", [
    ConversationState(HISTORY, None, 2, 2),
    ConversationState(HISTORY, SUMMARY, 0, 0),
    ConversationState(HISTORY, SUMMARY, 2, 0),
    ConversationState(HISTORY, None, 0, 1),
])
def test_store_rejects_invalid_v3_summary_and_boundaries(tmp_path, state):
    store = SessionStore(tmp_path / "session.json")
    with pytest.raises(SessionStoreError):
        store.save_state(state)
    assert not store.path.exists()


@pytest.mark.parametrize("payload", [
    "not-json",
    json.dumps({"version": 4, "messages": []}),
    json.dumps({"version": 3, "messages": [], "context": {"summary": None, "summary_through_message_count": 0, "context_start_message_count": 0}, "preferences": [], "extra": True}),
    json.dumps({"version": 1, "messages": [{"role": "assistant", "content": "orphan"}]}),
])
def test_store_rejects_corrupt_unsupported_or_invalid_history_without_writing(tmp_path, payload):
    path = tmp_path / "chat-session.json"
    path.write_text(payload)
    with pytest.raises(SessionStoreError):
        SessionStore(path).load_state()
    assert path.read_text() == payload


def test_store_rejects_extra_v3_context_key_and_duplicate_summary_field(tmp_path):
    path = tmp_path / "session.json"
    state = ConversationState(HISTORY, SUMMARY, 2, 2)
    store = SessionStore(path)
    store.save_state(state)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["context"]["unknown"] = "不应接受"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SessionStoreError):
        store.load_state()
    payload["context"].pop("unknown")
    raw = json.dumps(payload, ensure_ascii=False).replace('"goal": "继续开发"', '"goal": "继续开发", "goal": "冲突"')
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(SessionStoreError):
        store.load_state()


def test_store_cleans_temporary_file_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "data" / "chat-session.json"
    store = SessionStore(path)
    monkeypatch.setattr(session_store.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("detail")))
    with pytest.raises(SessionStoreError):
        store.save(HISTORY)
    assert not path.exists()
    assert list(path.parent.glob("*.tmp")) == []


def test_store_rejects_system_prompt_as_history(tmp_path):
    store = SessionStore(tmp_path / "chat-session.json")
    with pytest.raises(SessionStoreError):
        store.save((ChatMessage(ChatRole.SYSTEM, "rules"), ChatMessage(ChatRole.USER, "q"), ChatMessage(ChatRole.ASSISTANT, "a")))
