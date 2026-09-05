"""最终消息与流式临时文本的展示组件。"""

from rich import box
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from app.tui.widgets import SelectableRichLog


class Transcript(SelectableRichLog):
    """按角色渲染消息，保留原有选择与双击复制能力。"""

    def __init__(self) -> None:
        super().__init__(
            id="transcript", wrap=True, markup=False, auto_scroll=True,
            copy_line_on_double_click=True,
        )

    def write_message(self, role: str, content: str) -> None:
        """只为 Assistant 解析 Markdown，用户与系统原文保持纯文本。"""

        if role == "You":
            self.write(Panel(
                Text(content), title=Text("You", style="bold"),
                title_align="left", box=box.ROUNDED, border_style="#666666",
                style="#f2f2f2 on #2b2b2b", padding=(0, 1), expand=True,
            ))
        elif role == "Assistant":
            self.write(Text(role, style="bold"))
            self.write(Markdown(content))
        else:
            # 单独的片段样式避免正文继承角色标题的粗体。
            message = Text.assemble((role, "bold"), "\n", content)
            self.write(message)


class StreamOutput(SelectableRichLog):
    """维护待绘制的流式文本；请求有效性由主应用判断。"""

    def __init__(self) -> None:
        super().__init__(id="stream-output", wrap=True, markup=False, auto_scroll=True)
        self._fragments: list[str] = []
        self._dirty = False

    def append_delta(self, delta: str) -> None:
        """累计纯文本，在活动 Timer 中统一绘制以减少刷新频率。"""

        self._fragments.append(delta)
        self._dirty = True

    def flush(self) -> None:
        """只有新增内容时才重绘临时区。"""

        if not self._dirty:
            return
        self.clear()
        self.write(Text("".join(self._fragments)))
        self.display = True
        self._dirty = False

    def reset_output(self) -> None:
        """同时清空缓冲和画面，避免旧文本在下一次刷新时重现。"""

        self._fragments.clear()
        self._dirty = False
        self.clear()
        self.display = False
