"""通过 macOS 原生目录选择器取得项目路径，不修改项目配置。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from app.webui.projects import normalize_project_path


OSASCRIPT = Path("/usr/bin/osascript")
CHOOSE_FOLDER_SCRIPT = 'POSIX path of (choose folder with prompt "选择项目目录")'
PICKER_TIMEOUT_SECONDS = 120
_picker_lock = asyncio.Lock()


class DirectoryPickerError(Exception):
    """向页面提供稳定错误，不暴露系统脚本原始输出。"""


class DirectoryPickerUnavailable(DirectoryPickerError):
    """当前系统没有可用的目录选择器。"""


class DirectoryPickerBusy(DirectoryPickerError):
    """已有目录选择器正在等待用户操作。"""


def directory_picker_available() -> bool:
    """仅在本机 macOS 提供系统目录选择按钮。"""

    return sys.platform == "darwin" and OSASCRIPT.is_file()


async def pick_project_directory() -> str | None:
    """打开系统目录对话框；取消返回 None，选中路径仍须由用户保存。"""

    if not directory_picker_available():
        raise DirectoryPickerUnavailable("当前系统不支持目录选择，请手动输入路径。")
    if _picker_lock.locked():
        raise DirectoryPickerBusy("目录选择窗口已打开。")
    async with _picker_lock:
        try:
            process = await asyncio.create_subprocess_exec(
                str(OSASCRIPT), "-e", CHOOSE_FOLDER_SCRIPT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise DirectoryPickerError("无法打开系统目录选择窗口，请手动输入路径。") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=PICKER_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            # HTTP 请求结束后不能让系统对话框和子进程继续悬挂。
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise DirectoryPickerError("目录选择超时，请重试或手动输入路径。") from exc

        if process.returncode != 0:
            if b"-128" in stderr:
                return None
            raise DirectoryPickerError("目录选择失败，请手动输入路径。")
        try:
            if len(stdout) > 2048:
                raise ValueError("oversized path")
            selected = stdout.decode("utf-8").rstrip("\r\n")
            return normalize_project_path(selected)
        except (UnicodeError, ValueError) as exc:
            raise DirectoryPickerError("所选目录不可用，请手动输入有效路径。") from exc
