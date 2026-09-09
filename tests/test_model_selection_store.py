import json
import os
from pathlib import Path

import pytest

import app.runtime.model_selection_store as selection_store_module
from app.runtime.model_selection_store import (
    MAX_MODEL_SELECTION_BYTES,
    ModelSelection,
    ModelSelectionStore,
    ModelSelectionStoreError,
)


def test_missing_model_selection_returns_none(tmp_path):
    store = ModelSelectionStore(tmp_path / "model-selection.json")

    assert store.load() is None


def test_model_selection_round_trip_is_compact_private_and_replaceable(tmp_path):
    path = tmp_path / "data" / "model-selection.json"
    store = ModelSelectionStore(path)

    store.save(ModelSelection("deepseek", "deepseek-v4-flash"))

    assert store.load() == ModelSelection("deepseek", "deepseek-v4-flash")
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
    }
    assert path.stat().st_mode & 0o777 == 0o600

    store.save(ModelSelection("aliyun", "qwen3-max"))

    assert store.load() == ModelSelection("aliyun", "qwen3-max")
    assert path.stat().st_mode & 0o777 == 0o600
    assert not tuple(path.parent.glob(f".{path.name}.*.tmp"))


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"version": 2, "provider": "deepseek", "model": "deepseek-chat"},
        {"version": 1, "provider": "other", "model": "model"},
        {"version": 1, "provider": "deepseek", "model": ""},
        {"version": 1, "provider": "deepseek", "model": "bad\nmodel"},
        {"version": 1, "provider": "deepseek", "model": "model", "extra": 1},
    ],
)
def test_model_selection_rejects_invalid_payloads_without_leaking_path(
    tmp_path,
    payload,
):
    path = tmp_path / "secret-directory" / "model-selection.json"
    path.parent.mkdir()
    path.write_text(json.dumps(payload), encoding="utf-8")
    store = ModelSelectionStore(path)

    with pytest.raises(ModelSelectionStoreError) as raised:
        store.load()

    assert str(raised.value) == "Unable to load saved model selection"
    assert str(tmp_path) not in str(raised.value)


def test_model_selection_rejects_corrupt_non_utf8_and_oversized_files(tmp_path):
    path = tmp_path / "model-selection.json"
    store = ModelSelectionStore(path)

    for content in (b"not-json", b"\xff", b"x" * (MAX_MODEL_SELECTION_BYTES + 1)):
        path.write_bytes(content)
        with pytest.raises(ModelSelectionStoreError):
            store.load()


def test_model_selection_rejects_directory_and_symbolic_link(tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(ModelSelectionStoreError):
        ModelSelectionStore(directory).load()

    target = tmp_path / "target.json"
    target.write_text(
        '{"version":1,"provider":"deepseek","model":"deepseek-chat"}\n',
        encoding="utf-8",
    )
    link = tmp_path / "model-selection.json"
    link.symlink_to(target)
    with pytest.raises(ModelSelectionStoreError):
        ModelSelectionStore(link).load()


@pytest.mark.parametrize(
    "selection",
    [
        ModelSelection("other", "model"),
        ModelSelection("deepseek", ""),
        ModelSelection("deepseek", "bad,model"),
        ModelSelection("aliyun", "bad\tmodel"),
    ],
)
def test_model_selection_refuses_invalid_values_on_save(tmp_path, selection):
    store = ModelSelectionStore(tmp_path / "model-selection.json")

    with pytest.raises(ModelSelectionStoreError) as raised:
        store.save(selection)

    assert str(raised.value) == "Unable to save model selection"
    assert not store.path.exists()


def test_model_selection_save_refuses_existing_symbolic_link(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("preserve", encoding="utf-8")
    path = tmp_path / "model-selection.json"
    path.symlink_to(target)

    with pytest.raises(ModelSelectionStoreError):
        ModelSelectionStore(path).save(ModelSelection("deepseek", "model"))

    assert target.read_text(encoding="utf-8") == "preserve"
    assert path.is_symlink()


def test_model_selection_replace_failure_cleans_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "data" / "model-selection.json"
    store = ModelSelectionStore(path)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("sensitive detail")

    monkeypatch.setattr(selection_store_module.os, "replace", fail_replace)

    with pytest.raises(ModelSelectionStoreError) as raised:
        store.save(ModelSelection("deepseek", "model"))

    assert str(raised.value) == "Unable to save model selection"
    assert not path.exists()
    assert not tuple(path.parent.glob(f".{path.name}.*.tmp"))
