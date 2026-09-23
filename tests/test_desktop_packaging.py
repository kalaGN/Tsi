"""桌面 sidecar 的路径、认证和静态资源契约。"""

from pathlib import Path
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import local_paths
from app import application
from app import desktop_backend
from app.desktop_backend import prepare_desktop_environment


def test_desktop_paths_use_explicit_absolute_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("TSI_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("TSI_LOG_ROOT", str(tmp_path / "logs"))

    assert local_paths.data_root() == tmp_path / "data"
    assert local_paths.log_root() == tmp_path / "logs"

    monkeypatch.setenv("TSI_DATA_ROOT", "relative")
    with pytest.raises(ValueError):
        local_paths.data_root()


def test_desktop_environment_creates_private_state_before_app_import(tmp_path, monkeypatch):
    monkeypatch.setenv("TSI_DESKTOP_TOKEN", "a" * 32)
    monkeypatch.chdir(tmp_path)

    base = prepare_desktop_environment(tmp_path)

    assert base == tmp_path / "Library" / "Application Support" / "Tsi"
    assert Path.cwd() == base / "workspace"
    assert Path(local_paths.data_root()) == base / "data"
    assert Path(local_paths.log_root()) == base / "logs"
    assert (base / "data").stat().st_mode & 0o777 == 0o700


def test_desktop_environment_accepts_absolute_state_root_for_isolated_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("TSI_DESKTOP_TOKEN", "a" * 32)
    monkeypatch.setenv("TSI_DESKTOP_STATE_ROOT", str(tmp_path / "desktop-state"))
    monkeypatch.chdir(tmp_path)

    assert prepare_desktop_environment() == tmp_path / "desktop-state"

    monkeypatch.setenv("TSI_DESKTOP_STATE_ROOT", "relative")
    with pytest.raises(RuntimeError, match="绝对路径"):
        prepare_desktop_environment()


def test_desktop_backend_stops_when_parent_closes_stdin(monkeypatch):
    monkeypatch.setattr(desktop_backend.sys, "stdin", SimpleNamespace(buffer=BytesIO(b"")))
    server = SimpleNamespace(should_exit=False)

    desktop_backend.watch_parent_input(server)

    assert server.should_exit is True


def test_desktop_api_requires_startup_token_without_changing_plain_http(monkeypatch):
    monkeypatch.setattr(application, "configure_model_logging", lambda: None)
    monkeypatch.setenv("TSI_DESKTOP_TOKEN", "b" * 32)
    desktop = TestClient(application.create_app())

    assert desktop.get("/ui/api/missing").status_code == 401
    assert desktop.get("/ui/api/missing", headers={"X-Tsi-Desktop-Token": "b" * 32}).status_code == 404
    assert desktop.get("/ui").status_code == 200

    monkeypatch.delenv("TSI_DESKTOP_TOKEN")
    browser = TestClient(application.create_app())
    assert browser.get("/ui/api/missing").status_code == 404


def test_web_page_sends_desktop_token_only_when_tauri_provides_one():
    source = (Path(__file__).resolve().parents[1] / "app/webui/static/app.js").read_text(encoding="utf-8")
    assert 'if (window.__TSI_DESKTOP_TOKEN__) headers["X-Tsi-Desktop-Token"]' in source
