"""Codex 风格的 TUI 模型候选列表。"""

from rich.text import Text
from textual.widgets import Static

from app.services.llm.contracts import ModelOption


_MAX_VISIBLE_CANDIDATES = 7
_PROVIDER_NAMES = {"deepseek": "DeepSeek", "aliyun": "Aliyun"}


class ModelPalette(Static):
    """只维护模型候选与选中位置，不创建或替换 Provider。"""

    def __init__(self, *, id: str = "model-preview") -> None:
        super().__init__(id=id, markup=False)
        self._candidates: tuple[ModelOption, ...] = ()
        self._current: tuple[str, str] | None = None
        self._index = 0

    @property
    def is_open(self) -> bool:
        """候选快照存在时接管模型选择按键。"""

        return bool(self._candidates)

    def open(
        self,
        options: tuple[ModelOption, ...],
        current_provider: str,
        current_model: str,
    ) -> None:
        """打开候选快照，并优先定位当前实际模型。"""

        self._candidates = tuple(options)
        self._current = (current_provider, current_model)
        self._index = 0
        for index, option in enumerate(self._candidates):
            if (option.provider, option.model) == self._current:
                self._index = index
                break
        self._render_candidates()

    def move_selection(self, offset: int) -> None:
        """在完整候选中循环移动，并保持选中项处于可见窗口。"""

        if self._candidates:
            self._index = (self._index + offset) % len(self._candidates)
            self._render_candidates()

    def take_selection(self) -> ModelOption | None:
        """返回当前候选并关闭列表；是否可用由主应用判断。"""

        if not self._candidates:
            return None
        selected = self._candidates[self._index]
        self._close()
        return selected

    def dismiss(self) -> None:
        """关闭列表且不产生选择。"""

        self._close()

    def _close(self) -> None:
        self._candidates = ()
        self._current = None
        self._index = 0
        self._render_candidates()

    def _render_candidates(self) -> None:
        """只渲染选中项附近的有界纯文本窗口。"""

        content = Text()
        start = max(
            0,
            min(
                self._index - _MAX_VISIBLE_CANDIDATES // 2,
                len(self._candidates) - _MAX_VISIBLE_CANDIDATES,
            ),
        )
        visible = self._candidates[start : start + _MAX_VISIBLE_CANDIDATES]
        for offset, option in enumerate(visible):
            index = start + offset
            selected = index == self._index
            labels = []
            if (option.provider, option.model) == self._current:
                labels.append("当前")
            if not option.api_key_configured:
                labels.append("Key 缺失")
            suffix = f"  {' · '.join(labels)}" if labels else ""
            provider = _PROVIDER_NAMES.get(option.provider, option.provider.title())
            content.append(
                f"{'›' if selected else ' '} {provider} · {option.model}{suffix}\n",
                style="bold reverse" if selected else "",
            )
        self.update(content)
        self.display = self.is_open
