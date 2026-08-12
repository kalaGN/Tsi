"""只运行项目已确认固定命令的有界检查工具。"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import stat
import time
from pathlib import Path
from typing import Mapping

from tools.contracts import (
    ToolArgumentError,
    ToolDefinition,
    ToolErrorCode,
    ToolRejectedError,
)
from tools.workspace import WorkspacePolicy


class RunProjectCheckTool:
    """通过无 Shell 固定 argv 执行四种项目门禁。"""

    definition = ToolDefinition(
        "run_project_check",
        "运行 compile、test_all、pip_check 或 diff_check 固定项目检查",
        {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": ["compile", "test_all", "pip_check", "diff_check"],
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )

    def __init__(self, policy: WorkspacePolicy, timeout_seconds: float = 120) -> None:
        self.policy = policy
        self.timeout_seconds = timeout_seconds
        self._python_link = policy.root / ".venv" / "bin" / "python"
        self._python_identity = self._resolve_python_identity()

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        allowed_names = {"compile", "test_all", "pip_check", "diff_check"}
        if set(arguments) != {"name"} or arguments.get("name") not in allowed_names:
            raise ToolArgumentError()
        name = str(arguments["name"])
        if name == "diff_check":
            executable = shutil.which("git")
            if executable is None:
                raise ToolRejectedError(ToolErrorCode.CHECK_UNAVAILABLE)
            argv = (executable, "diff", "--check")
        else:
            python = self._verified_python()
            if python is None:
                raise ToolRejectedError(ToolErrorCode.CHECK_UNAVAILABLE)
            executable = str(python)
            suffix = {
                "compile": (
                    "-m",
                    "compileall",
                    "-q",
                    "main.py",
                    "app",
                    "tools",
                    "tests",
                ),
                "test_all": ("-m", "pytest", "-q"),
                "pip_check": ("-m", "pip", "check"),
            }[name]
            argv = (executable, *suffix)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "PYTHONUTF8": "1",
        }
        if name == "test_all":
            environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=self.policy.root, env=environment,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise ToolRejectedError(ToolErrorCode.CHECK_UNAVAILABLE) from exc
        try:
            output, truncated = await asyncio.wait_for(
                _read_process_output(process, 24 * 1024),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            await _stop_process(process)
            raise ToolRejectedError(ToolErrorCode.CHECK_TIMEOUT) from exc
        except asyncio.CancelledError:
            await _stop_process(process)
            raise
        return {
            "name": name,
            "exit_code": process.returncode,
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
            "output": _decode_output(output, 24 * 1024),
            "truncated": truncated,
        }

    def _resolve_python_identity(self) -> tuple[Path, int, int] | None:
        """允许标准 venv 链接，但在 Tool 生命周期内固定其真实目标。"""

        try:
            resolved = self._python_link.resolve(strict=True)
            details = resolved.stat()
        except OSError:
            return None
        if not stat.S_ISREG(details.st_mode) or not os.access(resolved, os.X_OK):
            return None
        return resolved, details.st_dev, details.st_ino

    def _verified_python(self) -> Path | None:
        identity = self._python_identity
        if identity is None:
            return None
        expected_path, expected_device, expected_inode = identity
        try:
            current_path = self._python_link.resolve(strict=True)
            details = current_path.stat()
        except OSError:
            return None
        if (
            current_path != expected_path
            or details.st_dev != expected_device
            or details.st_ino != expected_inode
            or not stat.S_ISREG(details.st_mode)
            or not os.access(current_path, os.X_OK)
        ):
            return None
        return current_path


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (AttributeError, ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError):
            process.kill()
        await process.wait()


async def _read_process_output(
    process: asyncio.subprocess.Process,
    maximum: int,
) -> tuple[bytes, bool]:
    """排空检查进程输出，同时限制驻留内存和 ToolResult 大小。"""

    if process.stdout is None:
        raise RuntimeError("process stdout is unavailable")
    retained = bytearray()
    truncated = False
    while True:
        chunk = await process.stdout.read(8192)
        if not chunk:
            break
        remaining = maximum - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            truncated = True
    await process.wait()
    return bytes(retained), truncated


def _decode_output(output: bytes, maximum_bytes: int) -> str:
    """生成有界 UTF-8 文本，避免非法字节的替换字符放大结果。"""

    text = output.decode("utf-8", errors="replace")
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return text
    truncated = encoded[:maximum_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return ""
