from enum import Enum


class RunStatus(str, Enum):
    READY = "Ready"
    THINKING = "Thinking"
    ERROR = "Error"
