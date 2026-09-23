"""模型密钥配置的存储边界。"""

import json
import os
from pathlib import Path

import pytest

from app.runtime.model_config_store import ModelConfigConflict, ModelConfigError, ModelConfigStore


def test_defaults_and_private_round_trip(tmp_path):
    path = tmp_path / "data" / "model-config.json"
    store = ModelConfigStore(path)
    initial = store.load()
    assert initial.revision == 0
    assert initial.public_payload()["providers"]["deepseek"]["api_key_configured"] is False

    saved = store.save(
        expected_revision=0, provider="deepseek", models=["deepseek-v4-flash", "custom-1"],
        api_key_action="set", api_key="secret-example",
    )
    assert saved.revision == 1
    assert store.load().environment()["DEEPSEEK_API_KEY"] == "secret-example"
    assert "secret-example" not in json.dumps(saved.public_payload())
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700

    kept = store.save(
        expected_revision=1, provider="deepseek", models=["custom-1"],
        api_key_action="keep",
    )
    assert kept.providers["deepseek"].api_key == "secret-example"
    cleared = store.save(
        expected_revision=2, provider="deepseek", models=["custom-1"],
        api_key_action="clear",
    )
    assert cleared.providers["deepseek"].api_key == ""


def test_conflict_and_invalid_input_preserve_previous_secret(tmp_path):
    store = ModelConfigStore(tmp_path / "model-config.json")
    store.save(expected_revision=0, provider="aliyun", models=["qwen3-max"], api_key_action="set", api_key="private")
    assert store.load().environment()["DASHSCOPE_API_KEY"] == "private"
    assert [(item.provider, item.model) for item in store.load().options() if item.provider == "aliyun"] == [
        ("aliyun", "qwen3-max"),
    ]
    original = store.path.read_bytes()

    with pytest.raises(ModelConfigConflict):
        store.save(expected_revision=0, provider="aliyun", models=["other"], api_key_action="clear")
    with pytest.raises(ModelConfigError):
        store.save(expected_revision=1, provider="aliyun", models=["bad\nname"], api_key_action="keep")
    with pytest.raises(ModelConfigError):
        store.save(expected_revision=1, provider="aliyun", models=["other"], api_key_action="set", api_key="")
    assert store.path.read_bytes() == original


def test_rejects_symlink_loose_permissions_and_duplicate_json_keys(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ModelConfigError):
        ModelConfigStore(link).load()
    missing_link = tmp_path / "missing-link.json"
    missing_link.symlink_to(tmp_path / "missing-target.json")
    with pytest.raises(ModelConfigError):
        ModelConfigStore(missing_link).save(
            expected_revision=0, provider="deepseek", models=["x"], api_key_action="set", api_key="secret",
        )

    path = tmp_path / "model-config.json"
    store = ModelConfigStore(path)
    store.save(expected_revision=0, provider="deepseek", models=["x"], api_key_action="set", api_key="secret")
    os.chmod(path, 0o644)
    with pytest.raises(ModelConfigError):
        store.load()
    os.chmod(path, 0o600)
    path.write_text('{"version":1,"version":1}', encoding="utf-8")
    with pytest.raises(ModelConfigError):
        store.load()
