"""TUI 任务计划确认；仅决定是否继续，不授予工具权限。"""

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RichLog

from app.runtime.task_preflight import TaskDecision
from app.tui.widgets import SelectableRichLog


class PlanConfirmationScreen(ModalScreen[bool]):
    """展示有界计划；默认聚焦取消，Esc 永远拒绝执行。"""

    BINDINGS = [
        Binding("y", "approve", "执行", priority=True),
        Binding("n", "reject", "取消", priority=True),
        Binding("escape", "reject", "取消", priority=True),
    ]
    CSS_PATH = "styles/approval.tcss"

    def __init__(self, decision: TaskDecision) -> None:
        super().__init__()
        if decision.kind != "planned":
            raise ValueError("plan confirmation requires planned decision")
        self.decision = decision

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-dialog"):
            yield Label("确认任务计划", id="approval-title")
            yield Label("计划确认不代表批准后续文件或脚本操作。", id="approval-paths")
            yield SelectableRichLog(id="approval-diff", wrap=True, markup=False)
            with Horizontal(id="approval-actions"):
                yield Button("取消 (N/Esc)", id="reject", variant="error")
                yield Button("执行 (Y)", id="approve", variant="success")

    def on_mount(self) -> None:
        lines = [self.decision.reason, "", "执行步骤："]
        lines.extend(
            f"{index}. {step.action}\n   产出：{step.deliverable}"
            for index, step in enumerate(self.decision.steps, 1)
        )
        self.query_one("#approval-diff", RichLog).write(Text("\n".join(lines)))
        self.query_one("#reject", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve")

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)
