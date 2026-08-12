"""工作区写操作的默认拒绝 Diff 审批界面。"""

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RichLog

from app.tui.widgets import SelectableRichLog
from tools import ToolApprovalRequest


class ToolApprovalScreen(ModalScreen[bool]):
    """显示纯文本完整 Diff，只有显式同意才返回 True。"""

    BINDINGS = [
        Binding("y", "approve", "应用", priority=True),
        Binding("n", "reject", "拒绝", priority=True),
        Binding("escape", "reject", "拒绝", priority=True),
    ]
    CSS = """
    ToolApprovalScreen { align: center middle; }
    #approval-dialog {
        width: 92%;
        height: 88%;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }
    #approval-title { height: 2; text-style: bold; }
    #approval-paths { height: auto; max-height: 5; }
    #approval-diff { height: 1fr; border: solid $primary-background; }
    #approval-actions { height: 3; align-horizontal: right; padding-top: 1; }
    #approval-actions Button { margin-left: 1; }
    """

    def __init__(self, request: ToolApprovalRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-dialog"):
            yield Label(
                f"{self.request.title} · {self.request.tool_name}",
                id="approval-title",
            )
            yield Label(
                "文件：" + "、".join(self.request.paths),
                id="approval-paths",
            )
            yield SelectableRichLog(id="approval-diff", wrap=False, markup=False)
            with Horizontal(id="approval-actions"):
                yield Button("拒绝 (N/Esc)", id="reject", variant="error")
                yield Button("应用 (Y)", id="approve", variant="success")

    def on_mount(self) -> None:
        # Text 对象确保模型生成的 Markdown/ANSI 只作为可选择文字展示。
        self.query_one("#approval-diff", RichLog).write(
            Text(self.request.diff_text)
        )
        self.query_one("#reject", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve")

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)
