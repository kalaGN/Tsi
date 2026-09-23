"""Tauri sidecar 入口：启动独立的本机 FastAPI 进程。"""

from __future__ import annotations

import multiprocessing
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path


READY_PREFIX = "TSI_READY:"
STARTUP_TIMEOUT_SECONDS = 20.0
TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


def watch_parent_input(server: object) -> None:
    """桌面宿主关闭 stdin 时让服务自行退出，避免单文件子进程残留。"""

    try:
        sys.stdin.buffer.readline()
    finally:
        server.should_exit = True


def prepare_desktop_environment(home: Path | None = None) -> Path:
    """在导入应用模块前固定私有状态目录与初始空工作区。"""

    token = os.environ.get("TSI_DESKTOP_TOKEN", "")
    if TOKEN_PATTERN.fullmatch(token) is None:
        raise RuntimeError("桌面启动令牌无效。")
    state_root = os.environ.get("TSI_DESKTOP_STATE_ROOT")
    if state_root and not Path(state_root).is_absolute():
        raise RuntimeError("桌面状态目录必须是绝对路径。")
    base = (
        Path(state_root)
        if state_root
        else (home or Path.home()) / "Library" / "Application Support" / "Tsi"
    )
    for directory in (base, base / "data", base / "logs", base / "workspace"):
        if directory.is_symlink():
            raise RuntimeError("桌面状态目录不可用。")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not directory.is_dir():
            raise RuntimeError("桌面状态目录不可用。")
    os.environ["TSI_DATA_ROOT"] = str(base / "data")
    os.environ["TSI_LOG_ROOT"] = str(base / "logs")
    from dotenv import load_dotenv

    config = base / ".env"
    if config.is_symlink():
        raise RuntimeError("桌面模型配置不可用。")
    load_dotenv(config, override=False)
    os.chdir(base / "workspace")
    return base


def serve() -> None:
    """绑定随机 loopback 端口，服务真正启动后才通知 Tauri。"""

    multiprocessing.freeze_support()
    prepare_desktop_environment()
    import uvicorn
    from main import app

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False)
    )
    worker = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="tsi-desktop-http",
        daemon=True,
    )
    worker.start()
    threading.Thread(
        target=watch_parent_input,
        args=(server,),
        name="tsi-desktop-parent-watch",
        daemon=True,
    ).start()
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    try:
        while not server.started:
            if not worker.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("桌面本地服务启动失败。")
            time.sleep(0.05)
        print(f"{READY_PREFIX}{port}", flush=True)
        worker.join()
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        listener.close()


if __name__ == "__main__":
    serve()
