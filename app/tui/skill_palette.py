"""输入框 `$技能名` 候选的定位、选择与展示。"""

import re
from dataclasses import dataclass
from typing import TypeAlias

from rich.text import Text
from textual.widgets import Static

from app.runtime.skill_runtime import AvailableSkill


_PARTIAL_SKILL_NAME = re.compile(r"^[a-z0-9-]*$")
_MAX_VISIBLE_CANDIDATES = 5
_MAX_DESCRIPTION_CHARACTERS = 80
Location: TypeAlias = tuple[int, int]


@dataclass(frozen=True)
class SkillMention:
    """光标所在的技能引用 token。"""

    start: Location
    end: Location
    prefix: str
    append_space: bool


@dataclass(frozen=True)
class SkillCompletion:
    """主应用可直接应用到 TextArea 的补全结果。"""

    start: Location
    end: Location
    text: str


def find_skill_mention(input_text: str, cursor: Location) -> SkillMention | None:
    """识别光标所在行、由输入起点或空白分隔的 `$前缀`。"""

    row, column = cursor
    lines = input_text.split("\n")
    if row < 0 or row >= len(lines):
        return None
    line = lines[row]
    if column < 0 or column > len(line):
        return None

    start_column = column
    while start_column > 0 and not line[start_column - 1].isspace():
        start_column -= 1
    end_column = column
    while end_column < len(line) and not line[end_column].isspace():
        end_column += 1

    token = line[start_column:end_column]
    prefix = line[start_column + 1 : column]
    if (
        not token.startswith("$")
        or column <= start_column
        or not _PARTIAL_SKILL_NAME.fullmatch(token[1:])
        or not _PARTIAL_SKILL_NAME.fullmatch(prefix)
    ):
        return None
    return SkillMention(
        start=(row, start_column),
        end=(row, end_column),
        prefix=prefix,
        append_space=end_column == len(line),
    )


class SkillPalette(Static):
    """保持输入框焦点，展示当前光标处的 Skill 候选。"""

    def __init__(self, *, id: str = "skill-preview") -> None:
        super().__init__(id=id, markup=False)
        self._candidates: list[AvailableSkill] = []
        self._mention: SkillMention | None = None
        self._index = 0
        self._dismissed_state: tuple[str, Location] | None = None

    @property
    def is_open(self) -> bool:
        """候选存在时才接管补全、方向键与 Esc。"""

        return bool(self._candidates)

    def filter_input(
        self,
        input_text: str,
        cursor: Location,
        skills: tuple[AvailableSkill, ...],
        *,
        enabled: bool,
    ) -> None:
        """按当前引用前缀过滤已发布的内存 Skill 摘要。"""

        state = (input_text, cursor)
        self._candidates = []
        self._mention = None
        self._index = 0
        if enabled and state != self._dismissed_state:
            mention = find_skill_mention(input_text, cursor)
            if mention is not None:
                self._mention = mention
                self._candidates = [
                    skill
                    for skill in skills
                    if skill.name.startswith(mention.prefix)
                    and skill.name != mention.prefix
                ]
        if state != self._dismissed_state:
            self._dismissed_state = None
        self._render_candidates()

    def move_selection(self, offset: int) -> None:
        """循环移动选中项。"""

        if self._candidates:
            self._index = (self._index + offset) % len(self._candidates)
            self._render_candidates()

    def take_selection(self) -> SkillCompletion | None:
        """消费当前候选并返回精确替换范围。"""

        if not self._candidates or self._mention is None:
            return None
        skill = self._candidates[self._index]
        completion = SkillCompletion(
            start=self._mention.start,
            end=self._mention.end,
            text=f"${skill.name}{' ' if self._mention.append_space else ''}",
        )
        self._candidates = []
        self._mention = None
        self._render_candidates()
        return completion

    def dismiss(self, input_text: str, cursor: Location) -> None:
        """关闭当前状态的候选，直到输入或光标发生变化。"""

        self._dismissed_state = (input_text, cursor)
        self._candidates = []
        self._mention = None
        self._render_candidates()

    def _render_candidates(self) -> None:
        """渲染有限的纯文本名称和描述列表。"""

        content = Text()
        start = max(
            0,
            min(
                self._index - _MAX_VISIBLE_CANDIDATES // 2,
                len(self._candidates) - _MAX_VISIBLE_CANDIDATES,
            ),
        )
        visible = self._candidates[start : start + _MAX_VISIBLE_CANDIDATES]
        for offset, skill in enumerate(visible):
            index = start + offset
            selected = index == self._index
            description = " ".join(skill.description.split())
            if len(description) > _MAX_DESCRIPTION_CHARACTERS:
                description = description[: _MAX_DESCRIPTION_CHARACTERS - 1] + "…"
            content.append(
                f"{'›' if selected else ' '} ${skill.name}  {description}\n",
                style="bold reverse" if selected else "",
            )
        self.update(content)
        self.display = self.is_open
