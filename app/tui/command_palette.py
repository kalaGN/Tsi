"""本地命令候选的过滤、选择和纯文本展示。"""

from rich.text import Text
from textual.widgets import Static


COMMANDS = (
    ("/clear", "清空对话、上下文和历史"),
    ("/skills", "查看可用技能"),
    ("/quit", "退出 TUI"),
)


class CommandPalette(Static):
    """保持输入框焦点，仅向应用返回用户选中的命令。"""

    def __init__(self, *, id: str = "command-preview") -> None:
        super().__init__(id=id, markup=False)
        self._candidates: list[tuple[str, str]] = []
        self._index = 0
        self._dismissed_text: str | None = None

    @property
    def is_open(self) -> bool:
        """候选存在时才接管补全与选择按键。"""

        return bool(self._candidates)

    def filter_input(self, value: str, *, enabled: bool) -> None:
        """按前缀更新候选；完整命令、空白和忙碌状态不弹出。"""

        self._candidates = []
        self._index = 0
        if (
            enabled
            and value != self._dismissed_text
            and value.startswith("/")
            and not any(character.isspace() for character in value)
            and value not in {name for name, _ in COMMANDS}
        ):
            self._candidates = [item for item in COMMANDS if item[0].startswith(value)]
        if value != self._dismissed_text:
            self._dismissed_text = None
        self._render_candidates()

    def move_selection(self, offset: int) -> None:
        """循环移动选中项，不触碰输入历史或草稿。"""

        if self._candidates:
            self._index = (self._index + offset) % len(self._candidates)
            self._render_candidates()

    def take_selection(self) -> str | None:
        """消费当前选择并关闭列表；命令执行仍由应用负责。"""

        if not self._candidates:
            return None
        command = self._candidates[self._index][0]
        self._candidates = []
        self._render_candidates()
        return command

    def dismiss(self, value: str) -> None:
        """记住已关闭的输入，避免同一输入的延迟事件重新打开候选。"""

        self._dismissed_text = value
        self._candidates = []
        self._render_candidates()

    def _render_candidates(self) -> None:
        """渲染有限的纯文本列表并高亮选择。"""

        content = Text()
        for index, (name, description) in enumerate(self._candidates):
            selected = index == self._index
            content.append(
                f"{'›' if selected else ' '} {name}  {description}\n",
                style="bold reverse" if selected else "",
            )
        self.update(content)
        self.display = self.is_open
