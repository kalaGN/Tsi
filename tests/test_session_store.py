import json
import stat

import pytest

from app.runtime import session_store
from app.runtime.session_store import SessionStore, SessionStoreError
from app.services.llm.contracts import ChatMessage, ChatRole


HISTORY = (
    ChatMessage(ChatRole.USER, "你好"),
    ChatMessage(ChatRole.ASSISTANT, "你好！"),
)


def test_missing_session_file_loads_empty_history(tmp_path):
    store = SessionStore(tmp_path / "nested" / "chat-session.json")

    assert store.load() == ()


def test_session_store_round_trips_utf8_and_uses_private_file_mode(tmp_path):
    path = tmp_path / "data" / "chat-session.json"
    store = SessionStore(path)

    store.save(HISTORY)

    assert store.load() == HISTORY
    assert "你好！" in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps({"version": 2, "messages": []}),
        json.dumps(
            {
                "version": 1,
                "messages": [{"role": "assistant", "content": "orphan"}],
            }
        ),
        json.dumps(
            {
                "version": 1,
                "messages": [{"role": "user", "content": ""}],
            }
        ),
    ],
)
def test_session_store_rejects_corrupt_or_unsupported_history(tmp_path, payload):
    path = tmp_path / "chat-session.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(SessionStoreError) as captured:
        SessionStore(path).load()

    assert str(captured.value) == "Unable to load saved conversation"
    assert path.read_text(encoding="utf-8") == payload


def test_session_store_cleans_temporary_file_when_replace_fails(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "data" / "chat-session.json"
    store = SessionStore(path)

    def fail_replace(source, destination):
        raise OSError("private filesystem detail")

    monkeypatch.setattr(session_store.os, "replace", fail_replace)

    with pytest.raises(SessionStoreError) as captured:
        store.save(HISTORY)

    assert str(captured.value) == "Unable to save conversation"
    assert not path.exists()
    assert list(path.parent.glob("*.tmp")) == []


def test_session_store_clear_removes_saved_history(tmp_path):
    path = tmp_path / "chat-session.json"
    store = SessionStore(path)
    store.save(HISTORY)

    store.clear()
    store.clear()

    assert not path.exists()
