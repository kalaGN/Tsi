"""为源码运行与桌面 sidecar 选择同一套本地状态目录。"""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def _root_from_env(name: str, fallback: str) -> Path:
    value = os.environ.get(name)
    if value is None:
        return PROJECT_ROOT / fallback
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def data_root() -> Path:
    """桌面构建使用用户数据目录；源码运行沿用项目 data/。"""

    return _root_from_env("TSI_DATA_ROOT", "data")


def log_root() -> Path:
    """桌面构建使用用户日志目录；源码运行沿用项目 logs/。"""

    return _root_from_env("TSI_LOG_ROOT", "logs")
