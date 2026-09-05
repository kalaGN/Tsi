"""与终端组件无关的输入历史导航状态。"""

from collections.abc import Iterable


class InputHistory:
    """保存已发送输入，并在历史浏览结束时恢复原始草稿。"""

    def __init__(self, entries: Iterable[str] = ()) -> None:
        self.entries = list(entries)
        self.index: int | None = None
        self._draft = ""

    def append(self, text: str) -> None:
        """记录已发送原文并结束上一轮历史浏览。"""

        self.entries.append(text)
        self.reset_navigation()

    def previous(self, draft: str) -> str | None:
        """首次向前浏览时保存草稿，抵达最早记录后保持不动。"""

        if not self.entries:
            return None
        if self.index is None:
            self._draft = draft
            self.index = len(self.entries) - 1
        else:
            self.index = max(0, self.index - 1)
        return self.entries[self.index]

    def next(self) -> str | None:
        """越过最新记录时返回草稿；未浏览时不替换输入。"""

        if self.index is None:
            return None
        if self.index < len(self.entries) - 1:
            self.index += 1
            return self.entries[self.index]
        draft = self._draft
        self.reset_navigation()
        return draft

    def reset_navigation(self) -> None:
        """结束导航并丢弃临时草稿，保留已发送记录。"""

        self.index = None
        self._draft = ""

    def clear(self) -> None:
        """在会话成功清理后重置全部历史状态。"""

        self.entries.clear()
        self.reset_navigation()
