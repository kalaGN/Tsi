"""TUI 专用可选择展示组件。"""

from rich.segment import Segment
from rich.style import Style
from textual.binding import Binding
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import RichLog, TextArea


class PromptTextArea(TextArea):
    """让输入框在标准和降级终端键盘协议下都可一键全选。"""

    BINDINGS = [
        Binding(
            "ctrl+a,super+a",
            "select_all",
            "Select all",
            show=False,
        )
    ]


class SelectableRichLog(RichLog):
    """补齐 RichLog 缺失的鼠标选择、高亮与文本提取能力。"""

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """从当前仍在 RichLog 中的渲染行提取可见文本。"""

        visible_text = "\n".join(line.text for line in self.lines)
        return selection.extract(visible_text), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        """选择范围变化时刷新高亮，不修改日志内容。"""

        self.refresh()

    def render_line(self, y: int) -> Strip:
        """为行附加选择坐标，并在选区内叠加 Screen 选择样式。"""

        scroll_x, scroll_y = self.scroll_offset
        content_y = scroll_y + y
        selection = self.text_selection

        if selection is None or content_y >= len(self.lines):
            return super().render_line(y).apply_offsets(scroll_x, content_y)

        width = self.scrollable_content_region.width
        line = self.lines[content_y].apply_style(self.rich_style)
        if (span := selection.get_span(content_y)) is not None:
            start, end = span
            line = _apply_character_style(
                line,
                start,
                len(line.text) if end == -1 else end,
                self.screen.get_component_rich_style("screen--selection"),
            )
        return line.crop_extend(
            scroll_x,
            scroll_x + width,
            self.rich_style,
        ).apply_offsets(scroll_x, content_y)


def _apply_character_style(
    strip: Strip,
    start: int,
    end: int,
    selection_style: Style,
) -> Strip:
    """按字符索引给 Strip 的局部 Segment 叠加样式，兼容中文宽字符。"""

    start = max(0, start)
    end = max(start, end)
    character_offset = 0
    styled_segments: list[Segment] = []

    for segment in strip:
        text, style, control = segment
        segment_end = character_offset + len(text)
        overlap_start = max(start, character_offset)
        overlap_end = min(end, segment_end)

        if control is not None or overlap_start >= overlap_end:
            styled_segments.append(segment)
        else:
            local_start = overlap_start - character_offset
            local_end = overlap_end - character_offset
            if local_start:
                styled_segments.append(Segment(text[:local_start], style))
            selected_style = (
                selection_style if style is None else style + selection_style
            )
            styled_segments.append(
                Segment(text[local_start:local_end], selected_style)
            )
            if local_end < len(text):
                styled_segments.append(Segment(text[local_end:], style))

        character_offset = segment_end

    return Strip(styled_segments, strip.cell_length)
