import asyncio
from pathlib import Path

import pytest
from textual.geometry import Offset
from textual.selection import SELECT_ALL, Selection
from textual.widgets import Footer, RichLog, Static, TextArea

from app.runtime.chat import (
    ChatErrorCode,
    ChatResult,
    ChatRuntimeError,
    ChatRuntimeInfo,
)
from app.runtime.session import ChatSession
from app.runtime.skill_runtime import SkillRuntime
from app.runtime.session_store import SessionStore, SessionStoreError
from app.services.llm.contracts import ChatMessage, ChatRole
from app.tui import __main__ as tui_main
from app.tui import application as tui_application
from app.tui.application import ChatTuiApp
from app.tui.state import RunStatus
from app.tui.approval import ToolApprovalScreen
from app.tui.widgets import SelectableRichLog
from tools import (
    SKILL_INSTALL_APPROVAL_WARNING_TEXT,
    ScriptApprovalRequest,
    SkillInstallApprovalRequest,
    ToolApprovalRequest,
    ToolCall,
    ToolResult,
)
from tools.workspace import WorkspacePolicy, create_workspace_registry
from tools.skills import load_skill_catalog


ALIYUN_INFO = ChatRuntimeInfo("aliyun", "qwen3-max", True)
DEEPSEEK_INFO = ChatRuntimeInfo("deepseek", "deepseek-v4-flash", True)
MISSING_KEY_INFO = ChatRuntimeInfo("deepseek", "deepseek-v4-flash", False)


class ManualClock:
    """为实时活动栏和退出窗口测试提供可重复读取的单调时钟。"""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def transcript_text(app: ChatTuiApp) -> str:
    return "\n".join(
        line.text for line in app.query_one("#transcript", RichLog).lines
    )


def test_command_preview_filters_selects_completes_and_executes_locally():
    async def scenario():
        received = []

        async def runner(text, **kwargs):
            received.append(text)
            return ChatResult("answer", "fake", "fake")

        app = ChatTuiApp(chat_runner=runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            preview = app.query_one("#command-preview", Static)
            await pilot.press("/")
            assert preview.display
            assert "/clear" in str(preview.content)
            await pilot.press("down", "tab")
            assert prompt.text == "/skills"
            assert not preview.display
            assert transcript_text(app) == ""
            await pilot.press("enter")
            assert "技能列表不可用" in transcript_text(app)
            await pilot.press("/", "s", "k")
            assert "/skills" in str(preview.content)
            assert "/clear" not in str(preview.content)
            await pilot.press("enter")
            assert prompt.text == "/skills"
            assert received == []
            assert app._input_history.entries == []

    asyncio.run(scenario())


def test_command_preview_escape_closes_before_clearing_input():
    async def scenario():
        async def runner(text, **kwargs):
            raise AssertionError("local interaction must not call model")

        app = ChatTuiApp(chat_runner=runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            preview = app.query_one("#command-preview", Static)
            await pilot.press("/")
            await pilot.press("escape")
            assert prompt.text == "/"
            assert not preview.display
            assert app._last_escape_at is None
            await pilot.press("escape")
            assert prompt.text == ""
            await pilot.press("/", "h")
            assert not preview.display

    asyncio.run(scenario())


def test_tui_uses_selectable_logs_for_transcript_and_stream_output():
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test():
            transcript = app.query_one("#transcript", SelectableRichLog)
            stream = app.query_one("#stream-output", SelectableRichLog)

            assert transcript.copy_line_on_double_click is True
            assert stream.copy_line_on_double_click is False

    asyncio.run(scenario())


def test_workspace_approval_modal_defaults_to_reject_and_supports_y(tmp_path):
    async def scenario():
        decisions = []
        request = ToolApprovalRequest(
            call_id="call-1",
            tool_name="apply_workspace_edits",
            title="应用修改",
            paths=("中文.txt",),
            diff_text="--- a/中文.txt\n+++ b/中文.txt\n@@ -0,0 +1 @@\n+内容\n",
            fingerprint="a" * 64,
        )

        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            decisions.append(await kwargs["on_tool_approval"](request))
            return ChatResult("完成", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=DEEPSEEK_INFO,
            workspace_registry=create_workspace_registry(WorkspacePolicy(tmp_path)),
        )
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("修改文件")
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, ToolApprovalScreen):
                    break
            assert isinstance(app.screen, ToolApprovalScreen)
            assert app.run_status is RunStatus.AWAITING_APPROVAL
            assert app.screen.query_one("#reject").has_focus
            await pilot.press("y")
            await app.workers.wait_for_complete()

            assert decisions == [True]
            assert app.run_status is RunStatus.READY
            assert "完成" in transcript_text(app)

    asyncio.run(scenario())


def test_skill_script_approval_shows_warning_command_and_execute_action():
    async def scenario():
        decisions = []
        request = ScriptApprovalRequest(
            call_id="script-1",
            tool_name="run_skill_script",
            title="执行 Skill 脚本",
            skill_name="demo-skill",
            script_path="scripts/run.py",
            command_text="python scripts/run.py '甲 乙'",
            warning_text="当前没有文件系统或网络沙箱。",
            fingerprint="c" * 64,
        )

        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            decisions.append(await kwargs["on_tool_approval"](request))
            return ChatResult("完成", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=DEEPSEEK_INFO,
            workspace_registry=create_workspace_registry(
                WorkspacePolicy(Path.cwd())
            ),
        )
        async with app.run_test() as pilot:
            app.query_one("#prompt", TextArea).load_text("执行脚本")
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, ToolApprovalScreen):
                    break
            modal = app.screen
            assert isinstance(modal, ToolApprovalScreen)
            preview = "\n".join(
                line.text
                for line in modal.query_one("#approval-diff", RichLog).lines
            )
            assert "没有文件系统或网络沙箱" in preview
            assert "python scripts/run.py '甲 乙'" in preview
            assert "Skill：demo-skill" in str(
                modal.query_one("#approval-paths", Static).content
            )
            assert "执行 (Y)" in str(modal.query_one("#approve").label)
            await pilot.press("y")
            await app.workers.wait_for_complete()

            assert decisions == [True]

    asyncio.run(scenario())


def test_skill_install_approval_shows_source_target_and_install_action():
    async def scenario():
        decisions = []
        request = SkillInstallApprovalRequest(
            call_id="install-1",
            tool_name="install_skill",
            title="安装 Skill",
            source_type="github",
            source_display="https://github.com/acme/repo/tree/main/skills/demo",
            target_path=".agents/skills/demo-skill",
            network_access=True,
            warning_text=SKILL_INSTALL_APPROVAL_WARNING_TEXT,
            fingerprint="d" * 64,
        )

        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            decisions.append(await kwargs["on_tool_approval"](request))
            return ChatResult("完成", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=DEEPSEEK_INFO,
            workspace_registry=create_workspace_registry(
                WorkspacePolicy(Path.cwd())
            ),
        )
        async with app.run_test() as pilot:
            app.query_one("#prompt", TextArea).load_text("安装技能")
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, ToolApprovalScreen):
                    break
            modal = app.screen
            assert isinstance(modal, ToolApprovalScreen)
            summary = str(modal.query_one("#approval-paths", Static).content)
            preview = "\n".join(
                line.text
                for line in modal.query_one("#approval-diff", RichLog).lines
            )
            assert "github.com/acme/repo" in summary
            assert ".agents/skills/demo-skill" in summary
            assert "访问网络：是" in summary
            assert "安装批准不会批准" in preview
            assert "安装 (Y)" in str(modal.query_one("#approve").label)
            await pilot.press("y")
            await app.workers.wait_for_complete()
            assert decisions == [True]

    asyncio.run(scenario())


@pytest.mark.parametrize("reject_action", ("n", "escape", "enter", "button"))
def test_workspace_approval_modal_rejects_without_exiting_main_app(
    tmp_path,
    reject_action,
):
    async def scenario():
        decisions = []
        request = ToolApprovalRequest(
            call_id="call-reject",
            tool_name="apply_workspace_edits",
            title="应用修改",
            paths=("demo.txt",),
            diff_text="+[bold]原样[/bold]\n+\x1b[31m红色标记\x1b[0m\n",
            fingerprint="b" * 64,
        )

        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            decisions.append(await kwargs["on_tool_approval"](request))
            return ChatResult("已拒绝", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=DEEPSEEK_INFO,
            workspace_registry=create_workspace_registry(WorkspacePolicy(tmp_path)),
        )
        async with app.run_test() as pilot:
            app.query_one("#prompt", TextArea).load_text("修改")
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, ToolApprovalScreen):
                    break
            modal = app.screen
            assert isinstance(modal, ToolApprovalScreen)
            diff_text = "\n".join(
                line.text for line in modal.query_one("#approval-diff", RichLog).lines
            )
            assert "[bold]原样[/bold]" in diff_text
            if reject_action == "button":
                await pilot.click("#reject")
            else:
                await pilot.press(reject_action)
            await app.workers.wait_for_complete()

            assert decisions == [False]
            assert app.run_status is RunStatus.READY
            assert "已拒绝" in transcript_text(app)

    asyncio.run(scenario())


def test_tui_reports_applied_paths_when_model_fails_after_write(tmp_path):
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            kwargs["on_tool_result"](
                ToolCall("write-1", "apply_workspace_edits", "{}"),
                ToolResult(
                    "write-1",
                    '{"ok":true,"data":{"change_id":"change-1","paths":["app/中文.py"]}}',
                ),
            )
            raise ChatRuntimeError(ChatErrorCode.UPSTREAM, "Upstream failed")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=DEEPSEEK_INFO,
            workspace_registry=create_workspace_registry(WorkspacePolicy(tmp_path)),
        )
        async with app.run_test() as pilot:
            app.query_one("#prompt", TextArea).load_text("修改")
            await pilot.press("enter")
            await app.workers.wait_for_complete()

            text = transcript_text(app)
            assert "Upstream failed" in text
            assert "本轮已写入但尚未完成：app/中文.py" in text

    asyncio.run(scenario())


def test_cmd_or_ctrl_a_selects_all_prompt_text():
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not be called during selection")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("第一行\n第二行")
            prompt.move_cursor((0, 1))
            prompt.focus()
            await pilot.pause()

            for shortcut in ("super+a", "ctrl+a"):
                prompt.move_cursor((0, 1))
                await pilot.press(shortcut)
                assert prompt.selected_text == "第一行\n第二行"

    asyncio.run(scenario())


def test_selectable_log_extracts_multiline_chinese_selection():
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test() as pilot:
            transcript = app.query_one("#transcript", SelectableRichLog)
            transcript.clear().write("你好\n世界")
            await pilot.pause()

            selection = Selection.from_offsets(Offset(1, 0), Offset(1, 1))

            assert transcript.get_selection(selection) == ("好\n世", "\n")

    asyncio.run(scenario())


def test_selectable_log_supports_mouse_drag_and_ctrl_c_copy():
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test() as pilot:
            transcript = app.query_one("#transcript", SelectableRichLog)
            transcript.clear().write("中文复制")
            await pilot.pause()

            assert await pilot.mouse_down(transcript, offset=(0, 0)) is True
            assert await pilot.mouse_up(transcript, offset=(4, 0)) is True
            await pilot.pause()
            await pilot.press("ctrl+c")

            assert app.screen.get_selected_text() == "中文"
            assert app.clipboard == "中文"
            app.copy_to_clipboard("")
            await pilot.press("super+c")
            assert app.clipboard == "中文"

    asyncio.run(scenario())


def test_transcript_double_click_copies_only_rendered_chinese_line():
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not be called during selection")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test() as pilot:
            transcript = app.query_one("#transcript", SelectableRichLog)
            transcript.clear().write("第一行\n第二行\n第三行")
            await pilot.pause()

            content_x, content_y = transcript.gutter.top_left
            assert await pilot.click(
                transcript,
                offset=(content_x + 1, content_y + 1),
                times=2,
            )
            await pilot.pause()

            assert app.clipboard == "第二行"
            assert app.screen.get_selected_text() == "第二行"

    asyncio.run(scenario())


def test_transcript_double_click_uses_vertical_scroll_offset():
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not be called during selection")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test(size=(80, 18)) as pilot:
            transcript = app.query_one("#transcript", SelectableRichLog)
            transcript.clear().write("\n".join(f"第{index}行" for index in range(20)))
            await pilot.pause()
            transcript.scroll_to(y=5, animate=False, force=True)
            await pilot.pause()

            content_x, content_y = transcript.gutter.top_left
            visible_line = transcript.scroll_offset.y
            assert await pilot.click(
                transcript,
                offset=(content_x + 1, content_y),
                times=2,
            )
            await pilot.pause()

            assert visible_line > 0
            assert app.clipboard == f"第{visible_line}行"

    asyncio.run(scenario())


def test_transcript_double_click_blank_line_keeps_clipboard_and_clears_select_all():
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not be called during selection")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test() as pilot:
            transcript = app.query_one("#transcript", SelectableRichLog)
            transcript.clear().write("有内容\n\n下一行")
            app.copy_to_clipboard("保留内容")
            await pilot.pause()

            content_x, content_y = transcript.gutter.top_left
            assert await pilot.click(
                transcript,
                offset=(content_x + 1, content_y + 1),
                times=2,
            )
            await pilot.pause()

            assert app.clipboard == "保留内容"
            assert app.screen.get_selected_text() is None

    asyncio.run(scenario())


def test_transcript_double_click_copies_rendered_markdown_without_source_markers():
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not be called during selection")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test() as pilot:
            transcript = app.query_one("#transcript", SelectableRichLog)
            transcript.clear()
            app._write_message("Assistant", "## 中文标题\n\n**重点内容**")
            await pilot.pause()

            target_line = next(
                index
                for index, line in enumerate(transcript.lines)
                if "中文标题" in line.text
            )
            expected = transcript.lines[target_line].text
            content_x, content_y = transcript.gutter.top_left
            assert await pilot.click(
                transcript,
                offset=(content_x + 1, content_y + target_line),
                times=2,
            )
            await pilot.pause()

            assert app.clipboard == expected
            assert "中文标题" in app.clipboard
            assert "##" not in app.clipboard

    asyncio.run(scenario())


def test_selectable_log_copies_rendered_markdown_text_with_builtin_action():
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test() as pilot:
            transcript = app.query_one("#transcript", SelectableRichLog)
            transcript.clear()
            app._write_message("Assistant", "## 中文标题\n\n**重点内容**")
            await pilot.pause()

            app.screen.selections = {transcript: SELECT_ALL}
            await pilot.pause()
            app.screen.action_copy_text()

            assert "Assistant" in app.clipboard
            assert "中文标题" in app.clipboard
            assert "重点内容" in app.clipboard
            assert "##" not in app.clipboard
            assert "**" not in app.clipboard

    asyncio.run(scenario())


def test_selectable_log_renders_visible_selection_highlight():
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test() as pilot:
            transcript = app.query_one("#transcript", SelectableRichLog)
            transcript.clear().write("中文")
            await pilot.pause()
            original_styles = [segment.style for segment in transcript.render_line(0)]

            app.screen.selections = {
                transcript: Selection.from_offsets(Offset(0, 0), Offset(1, 0))
            }
            await pilot.pause()
            selected_styles = [segment.style for segment in transcript.render_line(0)]

            assert selected_styles != original_styles
            assert transcript.get_selection(app.screen.selections[transcript]) == (
                "中",
                "\n",
            )

    asyncio.run(scenario())


def test_transcript_and_prompt_selection_use_semitransparent_gray():
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test():
            transcript_selection = app.screen.get_component_styles(
                "screen--selection"
            ).background
            prompt = app.query_one("#prompt", TextArea)
            prompt_selection = prompt.get_component_styles(
                "text-area--selection"
            ).background

            assert transcript_selection.hex6 == "#808080"
            assert prompt_selection.hex6 == "#808080"
            assert transcript_selection.a == pytest.approx(0.5)
            assert prompt_selection.a == pytest.approx(0.5)

    asyncio.run(scenario())


def test_cleared_selectable_log_cannot_copy_removed_message():
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test() as pilot:
            transcript = app.query_one("#transcript", SelectableRichLog)
            transcript.clear().write("已经清除的消息")
            await pilot.pause()
            app.screen.selections = {transcript: SELECT_ALL}
            await pilot.pause()

            transcript.clear()
            app.copy_to_clipboard("原剪贴板内容")
            app.screen.action_copy_text()

            assert app.clipboard == ""
            assert "已经清除的消息" not in app.clipboard

    asyncio.run(scenario())


def test_tui_initial_state_shows_provider_model_and_key_status():
    async def scenario():
        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test():
            status_bar = app.query_one("#status-bar", Static)

            assert app.run_status is RunStatus.READY
            assert "Aliyun" in str(status_bar.content)
            assert "qwen3-max" in str(status_bar.content)
            assert "Key: configured" in str(status_bar.content)
            assert "AGENTS: none" in str(status_bar.content)
            assert "Ready" in str(status_bar.content)

    asyncio.run(scenario())


def test_tui_uses_official_project_name_for_app_and_header():
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test():
            assert app.TITLE == "Tsi 助手"
            assert str(app.query_one("#title", Static).content) == "Tsi 助手"

    asyncio.run(scenario())


def test_tui_status_shows_loaded_system_prompt_without_exposing_content():
    async def scenario():
        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=DEEPSEEK_INFO,
            system_prompt="private project rules",
        )
        async with app.run_test():
            status = str(app.query_one("#status-bar", Static).content)

            assert "AGENTS: loaded" in status
            assert "private project rules" not in status
            assert "private project rules" not in transcript_text(app)

    asyncio.run(scenario())


def test_tui_system_prompt_error_blocks_normal_request_but_keeps_input():
    async def scenario():
        received_inputs = []

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            received_inputs.append(input_text)
            return ChatResult("unexpected", "fake", "fake-model")

        error = "Unable to load AGENTS.md"
        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=DEEPSEEK_INFO,
            system_prompt_error=error,
        )
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")

            await pilot.press("enter")

            assert received_inputs == []
            assert prompt.text == "hello"
            assert app._input_history.entries == []
            assert app.run_status is RunStatus.ERROR
            assert "AGENTS: error" in str(
                app.query_one("#status-bar", Static).content
            )
            assert error in transcript_text(app)

    asyncio.run(scenario())


def test_tui_initial_state_supports_deepseek_status():
    async def scenario():
        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test():
            status = str(app.query_one("#status-bar", Static).content)

            assert "DeepSeek" in status
            assert "deepseek-v4-flash" in status
            assert "Key: configured" in status

    asyncio.run(scenario())


def test_tui_skill_error_is_visible_but_does_not_block_chat():
    async def scenario():
        received = []

        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            received.append(input_text)
            return ChatResult("仍可对话", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=DEEPSEEK_INFO,
            skills_error="Project skills are unavailable",
        )
        async with app.run_test() as pilot:
            status = str(app.query_one("#status-bar", Static).content)
            assert "Skills: error" in status
            assert "Project skills are unavailable" in transcript_text(app)
            app.query_one("#prompt", TextArea).load_text("继续普通对话")
            await pilot.press("enter")
            await app.workers.wait_for_complete()

            assert received == ["继续普通对话"]
            assert "仍可对话" in transcript_text(app)

    asyncio.run(scenario())


def test_tui_refreshes_skill_count_from_runtime_after_request(tmp_path):
    async def scenario():
        runtime = SkillRuntime(
            tmp_path,
            None,
            WorkspacePolicy(tmp_path),
            load_skill_catalog(tmp_path),
            codex_skills_root=tmp_path / "codex-skills",
        )

        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            skill = tmp_path / ".agents/skills/demo-skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: demo-skill\ndescription: Demo\n---\n",
                encoding="utf-8",
            )
            runtime.publish(load_skill_catalog(tmp_path))
            return ChatResult("安装完成", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=DEEPSEEK_INFO,
            skill_runtime=runtime,
        )
        async with app.run_test() as pilot:
            assert "Skills: 0" in str(
                app.query_one("#status-bar", Static).content
            )
            app.query_one("#prompt", TextArea).load_text("安装技能")
            await pilot.press("enter")
            await app.workers.wait_for_complete()

            assert "Skills: 1" in str(
                app.query_one("#status-bar", Static).content
            )
            app.query_one("#prompt", TextArea).load_text("/skills")
            await pilot.press("enter")
            assert "demo-skill" in transcript_text(app)

    asyncio.run(scenario())


def test_activity_bar_starts_empty_above_prompt_without_footer():
    async def scenario():
        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test():
            activity = app.query_one("#activity-bar", Static)
            prompt = app.query_one("#prompt", TextArea)
            status = app.query_one("#status-bar", Static)

            assert str(activity.content) == ""
            assert activity.region.y < prompt.region.y < status.region.y
            assert not list(app.query(Footer))
            assert app._activity_timer is None

    asyncio.run(scenario())


def test_activity_bar_updates_elapsed_time_and_clears_after_success():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        clock = ManualClock(10.0)

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            started.set()
            await release.wait()
            return ChatResult("done", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=DEEPSEEK_INFO,
            clock=clock,
        )
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")
            await pilot.press("enter")
            await asyncio.wait_for(started.wait(), timeout=1)

            activity = app.query_one("#activity-bar", Static)
            initial = str(activity.content)
            assert "思考中" in initial
            assert "0.0 秒" in initial
            assert "Esc 取消" in initial
            assert app._activity_timer is not None

            clock.advance(1.2)
            await pilot.pause(0.15)
            updated = str(activity.content)
            assert "1.2 秒" in updated
            assert updated != initial

            release.set()
            await app.workers.wait_for_complete()

            assert str(activity.content) == ""
            assert app._activity_timer is None
            assert "System\n耗时：1.20 秒" in transcript_text(app)

    asyncio.run(scenario())


def test_tui_streams_plain_text_then_writes_final_markdown_once():
    async def scenario():
        streamed = asyncio.Event()
        release = asyncio.Event()

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            on_text_delta("**重")
            on_text_delta("要**")
            streamed.set()
            await release.wait()
            return ChatResult("**重要**", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")
            await pilot.press("enter")
            await asyncio.wait_for(streamed.wait(), timeout=1)
            await pilot.pause(0.15)

            stream_output = app.query_one("#stream-output", RichLog)
            assert stream_output.display is True
            assert "**重要**" in "\n".join(
                line.text for line in stream_output.lines
            )
            assert isinstance(stream_output, SelectableRichLog)
            app.screen.selections = {stream_output: SELECT_ALL}
            await pilot.pause()
            app.screen.action_copy_text()
            assert app.clipboard == "**重要**"
            assert "重要" not in transcript_text(app)

            release.set()
            await app.workers.wait_for_complete()

            assert stream_output.display is False
            assert stream_output.lines == []
            transcript = transcript_text(app)
            assert "重要" in transcript
            assert "**重要**" not in transcript

    asyncio.run(scenario())


def test_tui_resets_tool_step_stream_before_showing_final_step():
    async def scenario():
        first_streamed = asyncio.Event()
        continue_after_tool = asyncio.Event()
        final_streamed = asyncio.Event()
        release = asyncio.Event()

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            on_text_delta("工具中间文本")
            first_streamed.set()
            await continue_after_tool.wait()
            on_text_reset()
            on_text_delta("最终文本")
            final_streamed.set()
            await release.wait()
            return ChatResult("最终文本", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test() as pilot:
            app.query_one("#prompt", TextArea).load_text("hello")
            await pilot.press("enter")
            await asyncio.wait_for(first_streamed.wait(), timeout=1)
            await pilot.pause(0.15)
            stream_output = app.query_one("#stream-output", RichLog)
            assert "工具中间文本" in "\n".join(
                line.text for line in stream_output.lines
            )

            continue_after_tool.set()
            await asyncio.wait_for(final_streamed.wait(), timeout=1)
            await pilot.pause(0.15)
            current = "\n".join(line.text for line in stream_output.lines)
            assert "最终文本" in current
            assert "工具中间文本" not in current

            release.set()
            await app.workers.wait_for_complete()
            assert stream_output.display is False
            assert "最终文本" in transcript_text(app)

    asyncio.run(scenario())


def test_activity_bar_clears_after_known_and_unexpected_errors():
    async def scenario():
        async def known_error(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise ChatRuntimeError(
                ChatErrorCode.TIMEOUT,
                "Upstream request timed out",
            )

        async def unexpected_error(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise RuntimeError("internal detail")

        for runner in (known_error, unexpected_error):
            app = ChatTuiApp(chat_runner=runner, runtime_info=DEEPSEEK_INFO)
            async with app.run_test() as pilot:
                prompt = app.query_one("#prompt", TextArea)
                prompt.load_text("hello")
                await pilot.press("enter")
                await app.workers.wait_for_complete()

                assert str(app.query_one("#activity-bar", Static).content) == ""
                assert app._activity_timer is None

    asyncio.run(scenario())


def test_cancelled_worker_and_timer_cannot_clear_new_request_activity():
    async def scenario():
        first_started = asyncio.Event()
        first_cancelled = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()
        release_second = asyncio.Event()
        clock = ManualClock(10.0)
        call_count = 0

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                first_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    first_cancelled.set()
                    await release_first.wait()
                    return ChatResult("late first", "fake", "fake-model")

            second_started.set()
            await release_second.wait()
            return ChatResult("second answer", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=DEEPSEEK_INFO,
            clock=clock,
        )
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("first")
            await pilot.press("enter")
            await asyncio.wait_for(first_started.wait(), timeout=1)

            await pilot.press("escape")
            await asyncio.wait_for(first_cancelled.wait(), timeout=1)

            prompt.load_text("second")
            await pilot.press("enter")
            await asyncio.wait_for(second_started.wait(), timeout=1)
            assert app._last_escape_at is None
            activity = app.query_one("#activity-bar", Static)
            current_activity = str(activity.content)
            assert "思考中" in current_activity

            # 模拟已经排队的旧 Timer tick，并让吞掉取消的旧 Worker 返回。
            app._refresh_activity(1)
            assert str(activity.content) == current_activity
            release_first.set()
            await pilot.pause()
            assert "思考中" in str(activity.content)
            assert app._activity_generation == app._request_generation
            assert "late first" not in transcript_text(app)

            release_second.set()
            await app.workers.wait_for_complete()
            assert str(activity.content) == ""
            assert "second answer" in transcript_text(app)

    asyncio.run(scenario())


def test_tui_missing_key_starts_in_safe_error_state():
    async def scenario():
        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=MISSING_KEY_INFO)
        async with app.run_test():
            status_bar = app.query_one("#status-bar", Static)

            assert app.run_status is RunStatus.ERROR
            assert "Key: missing" in str(status_bar.content)
            assert "Upstream API key is not configured" in transcript_text(app)

    asyncio.run(scenario())


def test_tui_status_never_displays_api_key():
    async def scenario():
        secret_key = "test-secret-key-must-not-appear"

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test():
            status_content = str(app.query_one("#status-bar", Static).content)

            assert "Key: configured" in status_content
            assert secret_key not in status_content
            assert secret_key not in transcript_text(app)

    asyncio.run(scenario())


def test_tui_invalid_provider_configuration_starts_in_safe_error_state(monkeypatch):
    async def scenario():
        def fail_to_get_info():
            raise ChatRuntimeError(
                ChatErrorCode.CONFIGURATION,
                "Unsupported LLM provider configuration",
            )

        monkeypatch.setattr(
            tui_application,
            "get_chat_runtime_info",
            fail_to_get_info,
        )
        app = ChatTuiApp()

        async with app.run_test():
            assert app.run_status is RunStatus.ERROR
            assert "Unsupported LLM provider configuration" in transcript_text(app)

    asyncio.run(scenario())


def test_tui_entrypoint_loads_project_env_and_cwd_agents_once(
    tmp_path,
    monkeypatch,
):
    observed = {}
    system_prompt = "# 启动目录规则\n"
    (tmp_path / "AGENTS.md").write_text(system_prompt, encoding="utf-8")
    real_load_system_prompt = tui_main.load_system_prompt

    def observed_load_system_prompt(startup_directory):
        observed.setdefault("system_prompt_loads", []).append(startup_directory)
        return real_load_system_prompt(startup_directory)

    def fake_load_dotenv(path, override):
        observed["dotenv"] = (path, override)

    class FakeApp:
        def run(self):
            observed["run"] = True

    def fake_create_app(
        *,
        system_prompt,
        system_prompt_error,
        workspace_registry,
        workspace_error,
        skills_count,
        skills_error,
        skill_runtime,
    ):
        observed["kitty_keyboard_disabled"] = (
            tui_main.os.environ.get("TEXTUAL_DISABLE_KITTY_KEY")
        )
        observed["system_prompt"] = system_prompt
        observed["system_prompt_error"] = system_prompt_error
        observed["workspace_tools"] = tuple(
            definition.name for definition in workspace_registry.definitions
        )
        observed["workspace_error"] = workspace_error
        observed["skills_count"] = skills_count
        observed["skills_error"] = skills_error
        observed["skill_runtime"] = skill_runtime is not None
        return FakeApp()

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEXTUAL_DISABLE_KITTY_KEY", raising=False)
    monkeypatch.setattr(tui_main, "load_dotenv", fake_load_dotenv)
    monkeypatch.setattr(
        tui_main,
        "configure_model_logging",
        lambda **kwargs: observed.setdefault("logging", kwargs),
    )
    monkeypatch.setattr(
        tui_main,
        "load_system_prompt",
        observed_load_system_prompt,
    )
    monkeypatch.setattr(tui_main, "_create_app", fake_create_app)

    tui_main.main()

    expected_root = Path(tui_main.__file__).resolve().parents[2]
    assert observed == {
        "dotenv": (expected_root / ".env", False),
        "logging": {"enable_stream": False},
        "kitty_keyboard_disabled": "1",
        "system_prompt_loads": [tmp_path],
        "system_prompt": system_prompt,
        "system_prompt_error": None,
        "workspace_tools": (
            "get_current_time",
            "list_workspace_files",
            "search_workspace_text",
            "read_workspace_file",
            "get_workspace_git_status",
            "get_workspace_git_diff",
            "apply_workspace_edits",
            "run_project_check",
            "undo_workspace_change",
            "install_skill",
        ),
        "workspace_error": None,
        "skills_count": 0,
        "skills_error": None,
        "skill_runtime": True,
        "run": True,
    }


def test_tui_entrypoint_passes_safe_agents_error_to_app(tmp_path, monkeypatch):
    observed = {}
    (tmp_path / "AGENTS.md").write_bytes(b"\xff")

    class FakeApp:
        def run(self):
            observed["run"] = True

    def fake_create_app(
        *,
        system_prompt,
        system_prompt_error,
        workspace_registry,
        workspace_error,
        skills_count,
        skills_error,
        skill_runtime,
    ):
        observed["system_prompt"] = system_prompt
        observed["system_prompt_error"] = system_prompt_error
        observed["workspace_registry"] = workspace_registry is not None
        observed["workspace_error"] = workspace_error
        observed["skills_count"] = skills_count
        observed["skills_error"] = skills_error
        observed["skill_runtime"] = skill_runtime is not None
        return FakeApp()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tui_main, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(tui_main, "configure_model_logging", lambda **kwargs: None)
    monkeypatch.setattr(tui_main, "_create_app", fake_create_app)

    tui_main.main()

    assert observed == {
        "system_prompt": None,
        "system_prompt_error": (
            "AGENTS.md must be a readable UTF-8 file no larger than 32 KiB"
        ),
        "workspace_registry": True,
        "workspace_error": None,
        "skills_count": 0,
        "skills_error": None,
        "skill_runtime": True,
        "run": True,
    }


def test_tui_entrypoint_loads_skills_only_into_tui_registry(tmp_path, monkeypatch):
    skill_root = tmp_path / ".agents" / "skills" / "demo-skill"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: 演示能力\n---\n\n# 私有正文\n",
        encoding="utf-8",
    )
    observed = {}

    class FakeApp:
        def run(self):
            observed["run"] = True

    def fake_create_app(**kwargs):
        observed.update(kwargs)
        return FakeApp()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tui_main, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(tui_main, "configure_model_logging", lambda **kwargs: None)
    monkeypatch.setattr(tui_main, "_create_app", fake_create_app)

    tui_main.main()

    names = tuple(
        definition.name
        for definition in observed["workspace_registry"].definitions
    )
    assert names[-3:] == (
        "load_skill",
        "read_skill_resource",
        "run_skill_script",
    )
    assert observed["skills_count"] == 1
    assert observed["skills_error"] is None
    assert "demo-skill" in observed["system_prompt"]
    assert "演示能力" in observed["system_prompt"]
    assert "私有正文" not in observed["system_prompt"]
    assert observed["run"] is True


def test_tui_entrypoint_invalid_skill_keeps_workspace_tools(tmp_path, monkeypatch):
    skill_root = tmp_path / ".agents" / "skills" / "bad-skill"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("非法正文", encoding="utf-8")
    observed = {}

    class FakeApp:
        def run(self):
            observed["run"] = True

    def fake_create_app(**kwargs):
        observed.update(kwargs)
        return FakeApp()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tui_main, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(tui_main, "configure_model_logging", lambda **kwargs: None)
    monkeypatch.setattr(tui_main, "_create_app", fake_create_app)

    tui_main.main()

    names = {
        definition.name
        for definition in observed["workspace_registry"].definitions
    }
    assert "read_workspace_file" in names
    assert "load_skill" not in names
    assert "install_skill" in names
    assert observed["skills_count"] == 0
    assert observed["skills_error"] == "Project skills are unavailable"
    assert observed["system_prompt"] is None
    assert observed["run"] is True


def test_tui_entrypoint_reports_workspace_failure_without_path(tmp_path, monkeypatch):
    observed = {}

    class FakeApp:
        def run(self):
            observed["run"] = True

    def fail_registry(_policy, **_kwargs):
        raise ValueError(f"sensitive path: {tmp_path}")

    def fake_create_app(**kwargs):
        observed.update(kwargs)
        return FakeApp()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tui_main, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(tui_main, "configure_model_logging", lambda **kwargs: None)
    monkeypatch.setattr(tui_main, "create_workspace_registry", fail_registry)
    monkeypatch.setattr(tui_main, "_create_app", fake_create_app)

    tui_main.main()

    assert observed["workspace_registry"] is None
    assert observed["workspace_error"] == "Workspace tools are unavailable"
    assert str(tmp_path) not in observed["workspace_error"]
    assert observed["run"] is True


def test_enter_submits_input():
    async def scenario():
        received_inputs = []
        clock_values = iter([10.0, 11.234])

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            received_inputs.append(input_text)
            return ChatResult("answer", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=ALIYUN_INFO,
            clock=lambda: next(clock_values),
        )
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.focus()
            await pilot.press("h", "e", "l", "l", "o")

            await pilot.press("enter")
            await app.workers.wait_for_complete()

            assert received_inputs == ["hello"]
            assert prompt.text == ""
            assert app.run_status is RunStatus.READY
            transcript = transcript_text(app)
            assert "You" in transcript
            assert "hello" in transcript
            assert "Assistant\nanswer" in transcript
            assert "System\n耗时：1.23 秒" in transcript

    asyncio.run(scenario())


def test_up_down_navigates_input_history_and_restores_draft():
    async def scenario():
        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            return ChatResult(f"answer: {input_text}", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            for text in ("first", "second", "third"):
                prompt.load_text(text)
                await pilot.press("enter")
                await app.workers.wait_for_complete()

            prompt.load_text("未发送草稿")
            await pilot.press("up")
            assert prompt.text == "third"
            assert prompt.cursor_location == prompt.document.end

            await pilot.press("up")
            assert prompt.text == "second"
            await pilot.press("up")
            assert prompt.text == "first"
            await pilot.press("up")
            assert prompt.text == "first"

            await pilot.press("down")
            assert prompt.text == "second"
            await pilot.press("down")
            assert prompt.text == "third"
            await pilot.press("down")
            assert prompt.text == "未发送草稿"
            assert prompt.cursor_location == prompt.document.end
            await pilot.press("down")
            assert prompt.text == "未发送草稿"

    asyncio.run(scenario())


def test_empty_input_history_keeps_current_draft():
    async def scenario():
        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise AssertionError("runner should not be called")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("当前草稿")

            await pilot.press("up")
            await pilot.press("down")

            assert prompt.text == "当前草稿"
            assert app._input_history.index is None

    asyncio.run(scenario())


def test_input_history_restores_only_user_messages_with_original_text(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        store.save(
            (
                ChatMessage(ChatRole.USER, "中文\n多行"),
                ChatMessage(ChatRole.ASSISTANT, "不应进入输入历史"),
                ChatMessage(ChatRole.USER, "重复输入"),
                ChatMessage(ChatRole.ASSISTANT, "answer 2"),
                ChatMessage(ChatRole.USER, "重复输入"),
                ChatMessage(ChatRole.ASSISTANT, "answer 3"),
            )
        )
        app = ChatTuiApp(
            chat_session=ChatSession.load(store),
            runtime_info=ALIYUN_INFO,
        )

        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            await pilot.press("up")
            assert prompt.text == "重复输入"
            await pilot.press("up")
            assert prompt.text == "重复输入"
            await pilot.press("up")
            assert prompt.text == "中文\n多行"
            assert prompt.cursor_location == prompt.document.end

    asyncio.run(scenario())


def test_failed_input_remains_in_current_process_history():
    async def scenario():
        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise ChatRuntimeError(
                ChatErrorCode.TIMEOUT,
                "Upstream request timed out",
            )

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("失败输入")
            await pilot.press("enter")
            await app.workers.wait_for_complete()

            await pilot.press("up")
            assert prompt.text == "失败输入"

    asyncio.run(scenario())


def test_clear_failure_preserves_input_history(tmp_path, monkeypatch):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        store.save(
            (
                ChatMessage(ChatRole.USER, "保留输入"),
                ChatMessage(ChatRole.ASSISTANT, "answer"),
            )
        )
        session = ChatSession.load(store)

        def fail_clear():
            raise SessionStoreError("Unable to clear saved conversation")

        monkeypatch.setattr(store, "clear", fail_clear)
        app = ChatTuiApp(chat_session=session, runtime_info=ALIYUN_INFO)

        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("/clear")
            await pilot.press("enter")

            prompt.load_text("")
            await pilot.press("up")
            assert prompt.text == "保留输入"
            assert "Unable to clear saved conversation" in transcript_text(app)

    asyncio.run(scenario())


def test_assistant_output_renders_markdown():
    async def scenario():
        markdown_answer = """## 结果

- 第一项

> 中文引用

**重点**与[文档](https://example.com)

| 列 | 值 |
| --- | --- |
| A | B |

```python
print("hello")
```"""

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            return ChatResult(markdown_answer, "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("render markdown")

            await pilot.press("enter")
            await app.workers.wait_for_complete()

            transcript = transcript_text(app)
            assert "Assistant" in transcript
            assert "结果" in transcript
            assert "第一项" in transcript
            assert "中文引用" in transcript
            assert "重点" in transcript
            assert "文档" in transcript
            assert "A" in transcript
            assert "B" in transcript
            assert 'print("hello")' in transcript
            assert "## 结果" not in transcript
            assert "> 中文引用" not in transcript
            assert "**重点**" not in transcript
            assert "[文档](" not in transcript
            assert "| --- |" not in transcript
            assert "```python" not in transcript

    asyncio.run(scenario())


def test_restored_assistant_history_renders_markdown(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        store.save(
            (
                ChatMessage(ChatRole.USER, "上次的问题"),
                ChatMessage(ChatRole.ASSISTANT, "**重要回答**"),
            )
        )
        app = ChatTuiApp(
            chat_session=ChatSession.load(store),
            runtime_info=ALIYUN_INFO,
        )

        async with app.run_test():
            transcript = transcript_text(app)
            assert "Assistant" in transcript
            assert "重要回答" in transcript
            assert "**重要回答**" not in transcript
            assert app.chat_session.messages[1].content == "**重要回答**"
            assert SessionStore(store.path).load()[1].content == "**重要回答**"

    asyncio.run(scenario())


def test_non_assistant_messages_keep_markdown_markers_as_plain_text():
    async def scenario():
        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            return ChatResult("ok", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("# 用户标题 **原文**")

            await pilot.press("enter")
            await app.workers.wait_for_complete()
            app._write_message("System", "## 系统原文")
            app._write_message("Error", "**错误原文**")

            transcript = transcript_text(app)
            assert "# 用户标题 **原文**" in transcript
            assert "## 系统原文" in transcript
            assert "**错误原文**" in transcript

    asyncio.run(scenario())


def test_user_message_uses_background_card_without_styling_other_roles():
    async def scenario():
        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            return ChatResult("普通回答", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("用户 **原文**")

            await pilot.press("enter")
            await app.workers.wait_for_complete()

            lines = app.query_one("#transcript", RichLog).lines
            user_line = next(line for line in lines if "用户 **原文**" in line.text)
            user_segment = next(
                segment for segment in user_line if "用户 **原文**" in segment.text
            )
            assistant_line = next(line for line in lines if "普通回答" in line.text)
            assistant_segment = next(
                segment for segment in assistant_line if "普通回答" in segment.text
            )

            assert any("╭" in line.text and "You" in line.text for line in lines)
            assert user_segment.style is not None
            assert user_segment.style.color is not None
            assert user_segment.style.bgcolor is not None
            assert (
                assistant_segment.style is None
                or assistant_segment.style.bgcolor is None
            )
            assert "用户 **原文**" in transcript_text(app)

    asyncio.run(scenario())


def test_runtime_error_displays_request_duration():
    async def scenario():
        clock_values = iter([20.0, 22.5])

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise ChatRuntimeError(
                ChatErrorCode.TIMEOUT,
                "Upstream request timed out",
            )

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=ALIYUN_INFO,
            clock=lambda: next(clock_values),
        )
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")

            await pilot.press("enter")
            await app.workers.wait_for_complete()

            transcript = transcript_text(app)
            assert "Error\nUpstream request timed out" in transcript
            assert "System\n耗时：2.50 秒" in transcript

    asyncio.run(scenario())


def test_text_area_accepts_chinese_input_directly():
    async def scenario():
        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise AssertionError("runner should not be called while editing")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.focus()

            await pilot.press("中", "文", "输", "入")

            assert prompt.text == "中文输入"

    asyncio.run(scenario())


def test_blank_input_is_rejected_without_calling_runner():
    async def scenario():
        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise AssertionError("runner must not receive blank input")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("  \n")

            await pilot.press("enter")

            assert app.run_status is RunStatus.READY
            assert "Input must not be blank" in transcript_text(app)
            assert app._input_history.entries == []

    asyncio.run(scenario())


def test_help_and_chat_are_sent_as_ordinary_model_input():
    async def scenario():
        received_inputs = []

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            received_inputs.append(input_text)
            return ChatResult("ok", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            for text in ("/help", "/chat"):
                prompt.load_text(text)
                await pilot.press("enter")
                await app.workers.wait_for_complete()

            assert received_inputs == ["/help", "/chat"]

    asyncio.run(scenario())


def test_clear_command_clears_transcript_without_calling_runner():
    async def scenario():
        received_inputs = []

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            received_inputs.append(input_text)
            return ChatResult("answer", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert transcript_text(app)

            prompt.load_text(" /clear \n")
            await pilot.press("enter")

            assert transcript_text(app) == ""
            assert received_inputs == ["hello"]
            assert app.run_status is RunStatus.READY
            assert app._input_history.entries == []
            prompt.load_text("clear 后草稿")
            await pilot.press("up")
            assert prompt.text == "clear 后草稿"

    asyncio.run(scenario())


def test_skills_command_lists_current_runtime_without_calling_runner(tmp_path):
    async def scenario():
        received_inputs = []
        skill_root = tmp_path / ".agents/skills/demo-skill"
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: 演示技能\n---\n",
            encoding="utf-8",
        )
        runtime = SkillRuntime(
            tmp_path,
            None,
            WorkspacePolicy(tmp_path),
            load_skill_catalog(tmp_path),
            codex_skills_root=tmp_path / "codex-skills",
        )

        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            received_inputs.append(input_text)
            return ChatResult("answer", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=ALIYUN_INFO,
            skill_runtime=runtime,
        )
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text(" /skills \n")
            await pilot.press("enter")

            transcript = transcript_text(app)
            assert "可用技能（1）" in transcript
            assert "demo-skill" in transcript
            assert "演示技能" in transcript
            assert ".agents/skills/demo-skill/SKILL.md" in transcript
            assert prompt.text == ""
            assert received_inputs == []
            assert app._input_history.entries == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("runtime_factory", "expected"),
    (
        (
            lambda root: SkillRuntime(
                root,
                None,
                WorkspacePolicy(root),
                load_skill_catalog(root),
                codex_skills_root=root / "codex-skills",
            ),
            "当前没有可用技能",
        ),
        (lambda _root: None, "技能列表不可用"),
    ),
)
def test_skills_command_handles_empty_catalog_and_missing_runtime(
    tmp_path,
    runtime_factory,
    expected,
):
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not receive /skills")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=ALIYUN_INFO,
            skill_runtime=runtime_factory(tmp_path),
        )
        async with app.run_test() as pilot:
            app.query_one("#prompt", TextArea).load_text("/skills")
            await pilot.press("enter")

            assert expected in transcript_text(app)

    asyncio.run(scenario())


def test_skills_command_reports_safe_runtime_catalog_error(tmp_path):
    async def scenario():
        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not receive /skills")

        runtime = SkillRuntime(
            tmp_path,
            None,
            WorkspacePolicy(tmp_path),
            None,
            initial_error="Project skills are unavailable",
            codex_skills_root=tmp_path / "codex-skills",
        )
        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=ALIYUN_INFO,
            skill_runtime=runtime,
        )
        async with app.run_test() as pilot:
            app.query_one("#prompt", TextArea).load_text("/skills")
            await pilot.press("enter")

            transcript = transcript_text(app)
            assert "技能列表不可用" in transcript
            assert "Project skills are unavailable" in transcript

    asyncio.run(scenario())


def test_tui_restores_saved_conversation_on_mount(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        store.save(
            (
                ChatMessage(ChatRole.USER, "上次的问题"),
                ChatMessage(ChatRole.ASSISTANT, "上次的回答"),
            )
        )
        app = ChatTuiApp(
            chat_session=ChatSession.load(store),
            runtime_info=ALIYUN_INFO,
        )

        async with app.run_test():
            transcript = transcript_text(app)
            assert "You" in transcript
            assert "上次的问题" in transcript
            assert "Assistant\n上次的回答" in transcript

    asyncio.run(scenario())


def test_clear_command_removes_saved_conversation(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        store.save(
            (
                ChatMessage(ChatRole.USER, "question"),
                ChatMessage(ChatRole.ASSISTANT, "answer"),
            )
        )
        app = ChatTuiApp(
            chat_session=ChatSession.load(store),
            runtime_info=ALIYUN_INFO,
        )

        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("/clear")
            await pilot.press("enter")

            assert transcript_text(app) == ""
            assert app.chat_session.messages == ()
            assert not store.path.exists()

    asyncio.run(scenario())


def test_corrupt_history_requires_explicit_clear(tmp_path, monkeypatch):
    async def scenario():
        path = tmp_path / "chat-session.json"
        path.write_text("not-json", encoding="utf-8")
        store = SessionStore(path)
        monkeypatch.setattr(tui_application, "SessionStore", lambda: store)
        app = ChatTuiApp(runtime_info=ALIYUN_INFO)

        async with app.run_test() as pilot:
            assert app.run_status is RunStatus.ERROR
            assert "Unable to load saved conversation" in transcript_text(app)

            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("must not overwrite")
            await pilot.press("enter")
            assert path.read_text(encoding="utf-8") == "not-json"

            prompt.load_text("/clear")
            await pilot.press("enter")
            assert not path.exists()
            assert transcript_text(app) == ""
            assert app.run_status is RunStatus.READY

    asyncio.run(scenario())


def test_thinking_state_blocks_duplicate_submission():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        received_inputs = []

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            received_inputs.append(input_text)
            started.set()
            await release.wait()
            return ChatResult("done", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("first")
            await pilot.press("enter")
            await asyncio.wait_for(started.wait(), timeout=1)

            assert app.run_status is RunStatus.THINKING

            prompt.load_text("second")
            await pilot.press("enter")
            assert received_inputs == ["first"]
            assert prompt.text == "second"

            release.set()
            await app.workers.wait_for_complete()

            prompt.load_text("")
            await pilot.press("up")
            assert prompt.text == "first"
            await pilot.press("down")
            assert prompt.text == ""

    asyncio.run(scenario())


def test_runtime_error_is_recoverable_on_next_submission():
    async def scenario():
        attempts = 0

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ChatRuntimeError(
                    ChatErrorCode.TIMEOUT,
                    "Upstream request timed out",
                )
            return ChatResult("recovered", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("first")
            await pilot.press("enter")
            await app.workers.wait_for_complete()

            assert app.run_status is RunStatus.ERROR
            assert "Upstream request timed out" in transcript_text(app)

            prompt.load_text("second")
            await pilot.press("enter")
            await app.workers.wait_for_complete()

            assert app.run_status is RunStatus.READY
            assert "recovered" in transcript_text(app)

    asyncio.run(scenario())


def test_unexpected_error_is_hidden_and_app_remains_available():
    async def scenario():
        secret_detail = "internal-secret-detail"

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise RuntimeError(secret_detail)

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")
            await pilot.press("enter")
            await app.workers.wait_for_complete()

            transcript = transcript_text(app)
            assert app.run_status is RunStatus.ERROR
            assert "Unexpected internal error" in transcript
            assert secret_detail not in transcript

    asyncio.run(scenario())


def test_escape_clears_prompt_before_starting_exit_confirmation():
    async def scenario():
        clock = ManualClock(10.0)

        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            raise AssertionError("runner should not be called while clearing input")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=DEEPSEEK_INFO,
            clock=clock,
        )
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("待清空的中文\n第二行")

            await pilot.press("escape")

            assert prompt.text == ""
            assert app._last_escape_at is None
            assert "再次按 Esc 退出" not in transcript_text(app)
            assert app.is_running is True

            await pilot.press("escape")
            assert "再次按 Esc 退出" in transcript_text(app)

    asyncio.run(scenario())


def test_escape_clears_draft_before_cancelling_active_request():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_runner(input_text: str, **kwargs) -> ChatResult:
            started.set()
            await release.wait()
            return ChatResult("done", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("正在发送")
            await pilot.press("enter")
            await asyncio.wait_for(started.wait(), timeout=1)
            prompt.load_text("下一条草稿")

            await pilot.press("escape")

            assert prompt.text == ""
            assert app._active_worker is not None
            assert app.run_status is RunStatus.THINKING
            assert app._last_escape_at is None

            release.set()
            await app.workers.wait_for_complete()

    asyncio.run(scenario())


def test_double_escape_cancels_active_request_then_exits():
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()
        exit_called = False
        clock = ManualClock(10.0)

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                return ChatResult("late result", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=ALIYUN_INFO,
            clock=clock,
        )
        original_exit = app.exit

        def record_exit(*args, **kwargs):
            nonlocal exit_called
            exit_called = True
            return original_exit(*args, **kwargs)

        app.exit = record_exit
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            transcript_widget = app.query_one("#transcript", RichLog)
            prompt.load_text("hello")
            await pilot.press("enter")
            await asyncio.wait_for(started.wait(), timeout=1)

            clock.advance(0.2)
            await pilot.press("escape")
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            assert not exit_called
            assert str(app.query_one("#activity-bar", Static).content) == ""
            assert app._activity_timer is None
            assert "System\n再次按 Esc 退出" in transcript_text(app)
            assert "耗时：" not in transcript_text(app)

            prompt.load_text("")
            await pilot.press("up")
            assert prompt.text == "hello"

            clock.advance(0.1)
            await pilot.press("escape")
            assert prompt.text == ""
            assert app._last_escape_at is None
            assert not exit_called

            await pilot.press("escape")
            clock.advance(0.1)
            await pilot.press("escape")

        assert exit_called
        transcript = "\n".join(line.text for line in transcript_widget.lines)
        assert "耗时：" not in transcript

    asyncio.run(scenario())


def test_quit_command_exits_without_calling_runner():
    async def scenario():
        exit_called = False

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise AssertionError("runner must not receive /quit")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        original_exit = app.exit

        def record_exit(*args, **kwargs):
            nonlocal exit_called
            exit_called = True
            return original_exit(*args, **kwargs)

        app.exit = record_exit
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("/quit")
            await pilot.press("enter")

        assert exit_called
        assert app._input_history.entries == []

    asyncio.run(scenario())


def test_double_escape_exits_when_no_request_is_running():
    async def scenario():
        exit_called = False

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise AssertionError("runner should not be called")

        clock_values = iter([10.0, 10.5])
        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=ALIYUN_INFO,
            clock=lambda: next(clock_values),
        )
        original_exit = app.exit

        def record_exit(*args, **kwargs):
            nonlocal exit_called
            exit_called = True
            return original_exit(*args, **kwargs)

        app.exit = record_exit
        async with app.run_test() as pilot:
            await pilot.press("escape")
            assert not exit_called
            assert "再次按 Esc 退出" in transcript_text(app)

            await pilot.press("escape")

        assert exit_called

    asyncio.run(scenario())


def test_escape_confirmation_expires_after_timeout():
    async def scenario():
        exit_called = False
        clock_values = iter([10.0, 12.0, 12.5])

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            raise AssertionError("runner should not be called")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=ALIYUN_INFO,
            clock=lambda: next(clock_values),
        )
        original_exit = app.exit

        def record_exit(*args, **kwargs):
            nonlocal exit_called
            exit_called = True
            return original_exit(*args, **kwargs)

        app.exit = record_exit
        async with app.run_test() as pilot:
            await pilot.press("escape")
            await pilot.press("escape")
            assert not exit_called

            await pilot.press("escape")

        assert exit_called

    asyncio.run(scenario())


def test_quit_command_cancels_active_request_before_exit():
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()
        exit_called = False

        async def fake_runner(
            input_text: str,
            *,
            on_text_delta=None,
            on_text_reset=None,
        ) -> ChatResult:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        original_exit = app.exit

        def record_exit(*args, **kwargs):
            nonlocal exit_called
            exit_called = True
            return original_exit(*args, **kwargs)

        app.exit = record_exit
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")
            await pilot.press("enter")
            await asyncio.wait_for(started.wait(), timeout=1)

            prompt.load_text("/quit")
            await pilot.press("enter")

        assert exit_called
        assert cancelled.is_set()
        assert app._activity_timer is None

    asyncio.run(scenario())
