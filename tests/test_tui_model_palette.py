"""TUI 模型候选组件状态与纯文本展示测试。"""

import asyncio

from textual.app import App, ComposeResult

from app.services.llm.contracts import ModelOption
from app.tui.model_palette import ModelPalette


OPTIONS = (
    ModelOption("deepseek", "deepseek-v4-flash", True),
    ModelOption("aliyun", "qwen3-max", False),
)


class ModelPaletteTestApp(App[None]):
    def compose(self) -> ComposeResult:
        yield ModelPalette()


def test_model_palette_opens_on_current_model_and_marks_key_status() -> None:
    async def scenario() -> None:
        app = ModelPaletteTestApp()
        async with app.run_test():
            palette = app.query_one(ModelPalette)
            palette.open(OPTIONS, "deepseek", "deepseek-v4-flash")

            assert palette.is_open
            content = str(palette.content)
            assert "DeepSeek · deepseek-v4-flash" in content
            assert "当前" in content
            assert "Aliyun · qwen3-max" in content
            assert "Key 缺失" in content

    asyncio.run(scenario())


def test_model_palette_moves_cyclically_and_returns_selected_option() -> None:
    async def scenario() -> None:
        app = ModelPaletteTestApp()
        async with app.run_test():
            palette = app.query_one(ModelPalette)
            palette.open(OPTIONS, "deepseek", "deepseek-v4-flash")

            palette.move_selection(-1)
            assert palette.take_selection() == OPTIONS[-1]
            assert not palette.is_open

            palette.open(OPTIONS, "deepseek", "deepseek-v4-flash")
            palette.move_selection(1)
            assert palette.take_selection() == OPTIONS[1]

    asyncio.run(scenario())


def test_model_palette_dismisses_without_selection() -> None:
    async def scenario() -> None:
        app = ModelPaletteTestApp()
        async with app.run_test():
            palette = app.query_one(ModelPalette)
            palette.open(OPTIONS, "deepseek", "deepseek-v4-flash")

            palette.dismiss()

            assert not palette.is_open
            assert palette.take_selection() is None

    asyncio.run(scenario())


def test_model_palette_keeps_selected_item_visible_for_long_catalog() -> None:
    async def scenario() -> None:
        options = tuple(
            ModelOption("deepseek", f"model-{index}", True)
            for index in range(12)
        )
        app = ModelPaletteTestApp()
        async with app.run_test():
            palette = app.query_one(ModelPalette)
            palette.open(options, "deepseek", "model-0")
            for _ in range(11):
                palette.move_selection(1)

            content = str(palette.content)
            assert "model-11" in content
            assert content.count("DeepSeek ·") == 7

    asyncio.run(scenario())
