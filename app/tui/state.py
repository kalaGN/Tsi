"""TUI 可观察运行状态。"""

from enum import Enum


class RunStatus(str, Enum):
    """驱动状态栏展示的有限状态集合。"""

    READY = "Ready"
    THINKING = "Thinking"
    ERROR = "Error"
