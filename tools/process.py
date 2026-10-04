"""工作区命令与项目检查共用的有界进程控制。"""

import asyncio
import os
import signal


async def read_process_output(
    process: asyncio.subprocess.Process,
    maximum: int,
) -> tuple[bytes, bool]:
    """持续排空子进程管道，但只在内存保留固定字节数。"""

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


async def stop_process(process: asyncio.subprocess.Process) -> None:
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
