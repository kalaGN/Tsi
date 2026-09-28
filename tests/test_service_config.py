"""服务密钥必须私有保存，读取接口只暴露配置状态。"""

import json

import pytest

from app.runtime.service_config_store import (
    ServiceConfigConflict,
    ServiceConfigError,
    ServiceConfigStore,
    ServiceConfigValidation,
)


def test_service_config_store_round_trip_and_conflict(tmp_path):
    store = ServiceConfigStore(tmp_path / "data" / "service-config.json")
    assert store.load().public_payload()["services"][0]["fields"][0]["configured"] is False
    saved = store.save(expected_revision=0, service_id="serper", field_id="api_key", action="set", value="fake-serper-secret")
    assert saved.revision == 1
    assert store.secret("serper", "api_key") == "fake-serper-secret"
    assert "fake-serper-secret" not in json.dumps(saved.public_payload())
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ServiceConfigConflict):
        store.save(expected_revision=0, service_id="serper", field_id="api_key", action="clear")
    cleared = store.save(expected_revision=1, service_id="serper", field_id="api_key", action="clear")
    assert cleared.revision == 2
    assert store.secret("serper", "api_key") == ""


@pytest.mark.parametrize("invalid", ["", "has space", "bad\nline", "x" * 4097])
def test_service_config_rejects_bad_secret_without_replacing_file(tmp_path, invalid):
    store = ServiceConfigStore(tmp_path / "service-config.json")
    store.save(expected_revision=0, service_id="serper", field_id="api_key", action="set", value="good-fake-key")
    before = store.path.read_bytes()
    with pytest.raises(ServiceConfigValidation):
        store.save(expected_revision=1, service_id="serper", field_id="api_key", action="set", value=invalid)
    assert store.path.read_bytes() == before


def test_service_config_rejects_unsafe_or_corrupt_file(tmp_path):
    store = ServiceConfigStore(tmp_path / "service-config.json")
    store.path.write_text("broken", encoding="utf-8")
    store.path.chmod(0o600)
    with pytest.raises(ServiceConfigError):
        store.load()
    assert store.path.read_text(encoding="utf-8") == "broken"
    store.path.write_text('{"version":1,"version":1}', encoding="utf-8")
    with pytest.raises(ServiceConfigError):
        store.load()
    store.path.chmod(0o644)
    with pytest.raises(ServiceConfigError):
        store.load()
    store.path.chmod(0o600)
    tmp_path.chmod(0o755)
    with pytest.raises(ServiceConfigError):
        store.load()


def test_service_config_rejects_unknown_fields(tmp_path):
    store = ServiceConfigStore(tmp_path / "service-config.json")
    with pytest.raises(ServiceConfigValidation):
        store.save(expected_revision=0, service_id="other", field_id="api_key", action="set", value="fake-key")


def test_service_config_rejects_symlink_target(tmp_path):
    target = tmp_path / "real.json"
    target.write_text("not-a-config", encoding="utf-8")
    link = tmp_path / "service-config.json"
    link.symlink_to(target)
    store = ServiceConfigStore(link)
    with pytest.raises(ServiceConfigError):
        store.load()
    with pytest.raises(ServiceConfigError):
        store.save(expected_revision=0, service_id="serper", field_id="api_key", action="set", value="fake-key")
    assert target.read_text(encoding="utf-8") == "not-a-config"
