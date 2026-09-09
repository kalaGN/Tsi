"""TUI 可观察运行状态。"""

from dataclasses import dataclass
from enum import Enum


class RunStatus(str, Enum):
    """驱动状态栏展示的有限状态集合。"""

    READY = "Ready"
    THINKING = "Thinking"
    AWAITING_APPROVAL = "Awaiting approval"
    ERROR = "Error"


class IssueSeverity(str, Enum):
    """启动诊断对界面状态的影响程度。"""

    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class StartupIssue:
    """一项可安全展示的启动诊断及其发送阻断语义。"""

    code: str
    message: str
    severity: IssueSeverity
    blocks_prompt: bool


@dataclass(frozen=True)
class TuiHealthState:
    """集中保存启动诊断，避免界面维护多个相互分叉的错误字段。"""

    issues: tuple[StartupIssue, ...] = ()

    @property
    def first_blocking_issue(self) -> StartupIssue | None:
        return next((issue for issue in self.issues if issue.blocks_prompt), None)

    @property
    def has_error_status(self) -> bool:
        return any(
            issue.severity is IssueSeverity.ERROR for issue in self.issues
        )

    def without_code(self, code: str) -> "TuiHealthState":
        return TuiHealthState(
            tuple(issue for issue in self.issues if issue.code != code)
        )

    def replacing(self, issue: StartupIssue) -> "TuiHealthState":
        """按 code 原位替换诊断；不存在时追加到末尾。"""

        remaining = list(self.without_code(issue.code).issues)
        original_index = next(
            (
                index
                for index, current in enumerate(self.issues)
                if current.code == issue.code
            ),
            len(remaining),
        )
        remaining.insert(min(original_index, len(remaining)), issue)
        return TuiHealthState(tuple(remaining))
