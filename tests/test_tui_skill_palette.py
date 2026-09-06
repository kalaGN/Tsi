"""TUI Skill 候选解析与组件状态测试。"""

import asyncio

from textual.app import App, ComposeResult

from app.runtime.skill_runtime import AvailableSkill
from app.tui.skill_palette import SkillPalette, find_skill_mention


SKILLS = (
    AvailableSkill("alpha-skill", "Alpha", ".agents/skills/alpha-skill/SKILL.md"),
    AvailableSkill("demo-skill", "Demo", ".agents/skills/demo-skill/SKILL.md"),
)


class SkillPaletteTestApp(App[None]):
    def compose(self) -> ComposeResult:
        yield SkillPalette()


def test_find_skill_mention_supports_multiline_and_cursor_middle() -> None:
    text = "中文内容\n使用 $demo-old 完成"

    mention = find_skill_mention(text, (1, 6))

    assert mention is not None
    assert mention.start == (1, 3)
    assert mention.end == (1, 12)
    assert mention.prefix == "de"
    assert mention.append_space is False


def test_find_skill_mention_rejects_nonboundary_and_invalid_token() -> None:
    assert find_skill_mention("金额$demo", (0, 7)) is None
    assert find_skill_mention("使用 $demo.name", (0, 13)) is None
    assert find_skill_mention("使用 $demo", (0, 3)) is None
    assert find_skill_mention("普通文本", (0, 99)) is None


def test_skill_palette_filters_moves_completes_and_dismisses() -> None:
    async def scenario() -> None:
        app = SkillPaletteTestApp()
        async with app.run_test():
            palette = app.query_one(SkillPalette)
            palette.filter_input("请用 $", (0, 4), SKILLS, enabled=True)
            assert palette.is_open

            palette.move_selection(1)
            completion = palette.take_selection()
            assert completion is not None
            assert completion.start == (0, 3)
            assert completion.end == (0, 4)
            assert completion.text == "$demo-skill "
            assert not palette.is_open

            palette.filter_input("请用 $d", (0, 5), SKILLS, enabled=True)
            palette.dismiss("请用 $d", (0, 5))
            palette.filter_input("请用 $d", (0, 5), SKILLS, enabled=True)
            assert not palette.is_open
            palette.filter_input("请用 $de", (0, 6), SKILLS, enabled=True)
            assert palette.is_open

    asyncio.run(scenario())


def test_skill_palette_does_not_offer_complete_name_or_disabled_state() -> None:
    async def scenario() -> None:
        app = SkillPaletteTestApp()
        async with app.run_test():
            palette = app.query_one(SkillPalette)
            palette.filter_input("$demo-skill", (0, 11), SKILLS, enabled=True)
            assert not palette.is_open
            palette.filter_input("$", (0, 1), SKILLS, enabled=False)
            assert not palette.is_open

    asyncio.run(scenario())


def test_skill_palette_keeps_selection_visible_for_long_catalog() -> None:
    async def scenario() -> None:
        skills = tuple(
            AvailableSkill(
                f"skill-{index}",
                "多行\n描述" + "很长" * 50,
                f".agents/skills/skill-{index}/SKILL.md",
            )
            for index in range(7)
        )
        app = SkillPaletteTestApp()
        async with app.run_test():
            palette = app.query_one(SkillPalette)
            palette.filter_input("$", (0, 1), skills, enabled=True)
            for _ in range(6):
                palette.move_selection(1)

            assert "$skill-6" in str(palette.content)
            assert "\n描述" not in str(palette.content)
            assert str(palette.content).count("$skill-") == 5

    asyncio.run(scenario())
