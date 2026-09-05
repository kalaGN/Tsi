"""请求活动状态的纯展示组件，Timer 由应用管理。"""

from textual.widgets import Static

from app.tui.state import RunStatus


class ActivityBar(Static):
    """显示思考、审批等待与经过时间，不拥有请求生命周期。"""

    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self) -> None:
        super().__init__(id="activity-bar", markup=False)
        self._frame_index = 0

    def show_activity(self, elapsed: float, status: RunStatus, *, advance: bool = False) -> None:
        """使用应用提供的时间与状态绘制，刷新时可前进一帧。"""

        if advance:
            self._frame_index = (self._frame_index + 1) % len(self.FRAMES)
        label = "等待审批" if status is RunStatus.AWAITING_APPROVAL else "思考中"
        self.update(
            f"{self.FRAMES[self._frame_index]} {label} · {elapsed:.1f} 秒 · Esc 取消"
        )

    def reset_activity(self) -> None:
        """结束展示并复位动画，让下一次请求从第一帧开始。"""

        self._frame_index = 0
        self.update("")
