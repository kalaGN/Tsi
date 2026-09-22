"""本机系统目录选择器不触发真实 macOS 对话框的测试。"""

from __future__ import annotations

import asyncio

import pytest

from app.webui import directory_picker


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self.stdout, self.stderr


def test_system_picker_returns_normalized_existing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(directory_picker, "directory_picker_available", lambda: True)
    calls = []

    async def launch(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess((str(tmp_path) + "/\n").encode("utf-8"))

    monkeypatch.setattr(directory_picker.asyncio, "create_subprocess_exec", launch)
    assert asyncio.run(directory_picker.pick_project_directory()) == str(tmp_path)
    assert calls[0][0] == (str(directory_picker.OSASCRIPT), "-e", directory_picker.CHOOSE_FOLDER_SCRIPT)


def test_system_picker_cancellation_and_invalid_path_are_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(directory_picker, "directory_picker_available", lambda: True)

    async def cancelled(*_args, **_kwargs):
        return FakeProcess(b"", b"User canceled. (-128)", 1)

    monkeypatch.setattr(directory_picker.asyncio, "create_subprocess_exec", cancelled)
    assert asyncio.run(directory_picker.pick_project_directory()) is None

    async def invalid(*_args, **_kwargs):
        return FakeProcess(b"/\n")

    monkeypatch.setattr(directory_picker.asyncio, "create_subprocess_exec", invalid)
    with pytest.raises(directory_picker.DirectoryPickerError, match="所选目录不可用"):
        asyncio.run(directory_picker.pick_project_directory())


def test_system_picker_does_not_expose_script_error(monkeypatch):
    monkeypatch.setattr(directory_picker, "directory_picker_available", lambda: True)

    async def failed(*_args, **_kwargs):
        return FakeProcess(b"", b"private path and secret value", 1)

    monkeypatch.setattr(directory_picker.asyncio, "create_subprocess_exec", failed)
    with pytest.raises(directory_picker.DirectoryPickerError) as captured:
        asyncio.run(directory_picker.pick_project_directory())
    assert "private path" not in str(captured.value)


def test_system_picker_unavailable_keeps_manual_input(monkeypatch):
    monkeypatch.setattr(directory_picker, "directory_picker_available", lambda: False)
    with pytest.raises(directory_picker.DirectoryPickerUnavailable, match="手动输入"):
        asyncio.run(directory_picker.pick_project_directory())
