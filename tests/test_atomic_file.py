"""原子保存共用流程的权限与失败边界回归测试。"""

import stat

import pytest

from app.runtime.atomic_file import atomic_write


def test_atomic_write_replaces_content_with_private_permissions(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b"old")
    atomic_write(path, b"new", prefix=".config-")
    assert path.read_bytes() == b"new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_replace_failure_preserves_original_and_cleans_temporary(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_bytes(b"old")
    def fail_replace(*args):
        raise OSError("replace failed")
    monkeypatch.setattr("app.runtime.atomic_file.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write(path, b"new", prefix=".config-")
    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("target_type", ["symlink", "directory"])
def test_private_config_rejects_unsafe_target(tmp_path, target_type):
    path = tmp_path / "config.json"
    original = tmp_path / "original"
    original.write_bytes(b"old")
    if target_type == "symlink":
        path.symlink_to(original)
    else:
        path.mkdir()
    with pytest.raises(OSError, match="unsafe target"):
        atomic_write(path, b"new", prefix=".config-", check_target=True)
    assert original.read_bytes() == b"old"
    assert not list(tmp_path.glob(".config-*"))
