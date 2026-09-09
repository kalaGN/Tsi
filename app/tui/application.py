"""Textual 多轮对话界面、事件分发与状态投影。"""

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import Button, RichLog, Static, TextArea

from app.runtime.chat import ChatRuntimeError
from app.runtime.model_selection import ModelSelectionError
from app.services.llm.contracts import (
    ChatRole,
    ModelOption,
)
from app.tui.activity_bar import ActivityBar
from app.tui.approval import ToolApprovalScreen
from app.tui.bootstrap import TuiDependencies
from app.tui.command_palette import CommandPalette
from app.tui.commands import LocalCommand, parse_local_command
from app.tui.input_history import InputHistory
from app.tui.model_palette import ModelPalette
from app.tui.skill_palette import SkillPalette
from app.tui.request import RequestCoordinator
from app.tui.state import (
    IssueSeverity,
    RunStatus,
    StartupIssue,
)
from app.tui.status_bar import StatusBar, StatusBarState, provider_display_name
from app.tui.transcript import StreamOutput, Transcript
from app.tui.widgets import PromptTextArea


class ChatTuiApp(App[None]):
    """复用 Chat Runtime 的本地全屏多轮对话界面。"""

    TITLE = "Tsi 助手"
    ESCAPE_CONFIRM_SECONDS = 1.5
    ACTIVITY_INTERVAL_SECONDS = 0.1
    BINDINGS = [
        Binding("tab", "complete_suggestion", "Complete", show=False, priority=True),
        Binding("enter", "submit_prompt", "Send", show=False, priority=True),
        Binding("escape", "confirm_exit", "Exit (x2)", show=False, priority=True),
        Binding(
            "up",
            "previous_input",
            "Previous input",
            show=False,
            priority=True,
        ),
        Binding(
            "down",
            "next_input",
            "Next input",
            show=False,
            priority=True,
        ),
    ]
    CSS_PATH = "styles/application.tcss"

    run_status = reactive(RunStatus.READY)

    def __init__(
        self,
        dependencies: TuiDependencies,
    ) -> None:
        """使用已完成启动装配的有限依赖初始化 Textual 界面。"""

        super().__init__()
        self.clock = dependencies.clock
        self.runtime_info = dependencies.runtime_info
        self.chat_session = dependencies.chat_session
        self._model_selection = dependencies.model_selection
        self._skill_runtime = dependencies.skill_runtime
        self._health = dependencies.health
        self._system_prompt_loaded = dependencies.system_prompt_loaded
        self._workspace_enabled = dependencies.workspace_enabled
        self._skills_count = dependencies.skills_count
        self._last_escape_at: float | None = None
        self._request = RequestCoordinator(
            self,
            dependencies.chat_runner,
            clock=dependencies.clock,
            workspace_enabled=dependencies.workspace_enabled,
            activity_interval_seconds=self.ACTIVITY_INTERVAL_SECONDS,
        )
        self._input_history = InputHistory(
            [
                message.content
                for message in dependencies.chat_session.messages
                if message.role is ChatRole.USER
            ]
            if dependencies.chat_session is not None
            else []
        )

    def compose(self) -> ComposeResult:
        """声明标题、对话记录、输入框和状态栏布局。"""

        yield Static(self.TITLE, id="title", markup=False)
        yield Transcript()
        yield StreamOutput()
        yield ActivityBar()
        yield CommandPalette()
        yield SkillPalette()
        yield ModelPalette()
        yield PromptTextArea(
            id="prompt",
            soft_wrap=True,
            placeholder="Type a message. Enter: send, Esc x2: exit",
        )
        yield StatusBar()

    def on_mount(self) -> None:
        """挂载后恢复历史，并把配置或历史错误转换为安全界面状态。"""

        self.query_one("#prompt", TextArea).focus()
        if self.chat_session is not None:
            for message in self.chat_session.messages:
                role = "You" if message.role is ChatRole.USER else "Assistant"
                self._write_message(role, message.content)
        configuration_issue = self._issue_message("configuration")
        if configuration_issue is None and not self.runtime_info.api_key_configured:
            self.run_status = RunStatus.ERROR
            self._write_message("Error", "Upstream API key is not configured")
        for issue in self._health.issues:
            if issue.severity is IssueSeverity.ERROR:
                self.run_status = RunStatus.ERROR
                role = "Error"
            else:
                role = "System"
            self._write_message(role, issue.message)
            if issue.code == "history":
                self._write_message(
                    "System",
                    "Use /clear to reset saved conversation",
                )
        self._update_status_bar()

    def watch_run_status(self) -> None:
        """响应运行状态变化，并在组件挂载后刷新状态栏。"""

        if self.is_mounted:
            self._update_status_bar()

    def _update_status_bar(self) -> None:
        """显示 Provider、模型、密钥是否配置以及当前运行状态。"""

        self.query_one(StatusBar).show_status(
            StatusBarState(
                provider=self.runtime_info.provider,
                model=self.runtime_info.model,
                api_key_configured=self.runtime_info.api_key_configured,
                system_prompt_loaded=self._system_prompt_loaded,
                system_prompt_error=self._issue_message("system_prompt"),
                workspace_enabled=self._workspace_enabled,
                workspace_error=self._issue_message("workspace"),
                skills_count=self._skills_count,
                skills_error=self._issue_message("skills"),
                run_status=self.run_status,
            )
        )

    def _write_message(self, role: str, content: str) -> None:
        """将消息交给展示组件，应用只协调消息产生的时机。"""

        self.query_one(Transcript).write_message(role, content)

    def _issue_message(self, code: str) -> str | None:
        issue = next(
            (issue for issue in self._health.issues if issue.code == code),
            None,
        )
        return issue.message if issue is not None else None

    def write_request_message(self, role: str, content: str) -> None:
        self._write_message(role, content)

    def refresh_request_skill_status(self) -> None:
        self._refresh_skill_status()

    def focus_request_prompt(self) -> None:
        if self.is_mounted:
            self.query_one("#prompt", TextArea).focus()

    def action_submit_prompt(self) -> None:
        """处理本地命令、输入校验，并启动唯一的异步对话请求。"""

        if isinstance(self.screen, ToolApprovalScreen):
            self._submit_focused_approval_action()
            return
        prompt_widget = self.query_one("#prompt", TextArea)
        model_palette = self.query_one(ModelPalette)
        if model_palette.is_open:
            selection = model_palette.take_selection()
            if selection is not None:
                self._switch_model(selection)
            return
        if self.query_one(CommandPalette).is_open:
            self.action_complete_suggestion()
            return
        if self.query_one(SkillPalette).is_open:
            self.action_complete_suggestion()
            return
        input_text = prompt_widget.text
        command = parse_local_command(input_text)
        if command is LocalCommand.QUIT:
            self._request.cancel(show_message=False)
            self.exit()
            return
        if self._request.is_active:
            return
        if self._handle_local_command(command, prompt_widget):
            return
        if not self._can_start_prompt(input_text):
            return
        self._start_prompt_request(input_text, prompt_widget)

    def _submit_focused_approval_action(self) -> None:
        """把 Enter 交给审批界面当前聚焦的按钮。"""

        focused = self.screen.focused
        if isinstance(focused, Button):
            focused.press()

    def _handle_local_command(
        self,
        command: LocalCommand | None,
        prompt: TextArea,
    ) -> bool:
        """在空闲状态执行本地命令，并报告输入是否已被消费。"""

        if command is LocalCommand.CLEAR:
            self._clear_conversation(prompt)
            return True
        if command is LocalCommand.MEMORY:
            prompt.load_text("")
            self._write_memory_preferences()
            return True
        if command is LocalCommand.MEMORY_CLEAR:
            prompt.load_text("")
            self._clear_memory_preferences()
            return True
        if command is LocalCommand.MODEL:
            self._open_model_palette(prompt)
            return True
        if command is LocalCommand.SKILLS:
            prompt.load_text("")
            self._write_available_skills()
            return True
        return False

    def _write_memory_preferences(self) -> None:
        """显示长期偏好摘要，不展示滚动会话摘要。"""

        if self.chat_session is None or not self.chat_session.preferences:
            self._write_message("System", "尚未保存长期偏好")
            return
        lines = [f"长期偏好（{len(self.chat_session.preferences)}）："]
        lines.extend(
            f"{index}. {item.content}"
            for index, item in enumerate(self.chat_session.preferences, start=1)
        )
        self._write_message("System", "\n".join(lines))

    def _clear_memory_preferences(self) -> None:
        """原子清除长期偏好，不改变对话和摘要。"""

        if self.chat_session is None:
            self._write_message("System", "尚未保存长期偏好")
            return
        try:
            self.chat_session.clear_preferences()
        except ChatRuntimeError as exc:
            self._write_message("Error", exc.user_message)
            self.run_status = RunStatus.ERROR
            return
        self._write_message("System", "长期偏好已清除")

    def _clear_conversation(self, prompt: TextArea) -> None:
        """先清理持久化会话，成功后再重置界面与内存历史。"""

        try:
            if self.chat_session is not None:
                self.chat_session.clear()
        except ChatRuntimeError as exc:
            self._write_message("Error", exc.user_message)
            self.run_status = RunStatus.ERROR
            return
        self._health = self._health.without_code("history")
        self._input_history.clear()
        self._request.clear_transient_output()
        prompt.load_text("")
        self.query_one("#transcript", RichLog).clear()
        self.run_status = self._idle_status()

    def _can_start_prompt(self, input_text: str) -> bool:
        """按既有优先级展示阻断原因，避免启动无效请求。"""

        blocking_issue = self._health.first_blocking_issue
        if blocking_issue is not None:
            self._write_message("Error", blocking_issue.message)
            if blocking_issue.code == "history":
                self._write_message(
                    "System",
                    "Use /clear to reset saved conversation",
                )
            self.run_status = RunStatus.ERROR
            return False

        if not input_text.strip():
            self._write_message("System", "Input must not be blank")
            return False
        return True

    def _start_prompt_request(self, input_text: str, prompt: TextArea) -> None:
        """提交已验证输入，并把请求生命周期交给协调器。"""

        # 新请求重新开始双 Esc 手势，避免上一次取消被误判为本次的退出确认。
        self._last_escape_at = None
        self._input_history.append(input_text)
        prompt.load_text("")
        self._request.start(input_text)

    def _idle_status(self) -> RunStatus:
        if not self.runtime_info.api_key_configured or self._health.has_error_status:
            return RunStatus.ERROR
        return RunStatus.READY

    def _write_available_skills(self) -> None:
        """展示当前已发布 Skill 摘要，不触发模型请求或磁盘扫描。"""

        if self._skill_runtime is None:
            self._write_message("System", "技能列表不可用。")
            return
        status = self._skill_runtime.status()
        if status.error is not None:
            self._write_message("System", f"技能列表不可用：{status.error}")
            return
        skills = self._skill_runtime.available_skills()
        if not skills:
            self._write_message("System", "当前没有可用技能。")
            return
        lines = [f"可用技能（{len(skills)}）："]
        for skill in skills:
            lines.extend(
                (
                    f"- {skill.name}",
                    f"  描述：{skill.description}",
                    f"  入口：{skill.relative_entrypoint}",
                )
            )
        self._write_message("System", "\n".join(lines))

    def _open_model_palette(self, prompt: TextArea) -> None:
        """清空本地命令并用启动配置快照打开模型选择器。"""

        prompt.load_text("")
        if self.chat_session is None:
            self._write_message("System", "模型切换不可用。")
            return
        if self._model_selection is None or not self._model_selection.options:
            self._write_message("System", "当前没有可选模型。")
            return
        self.query_one(ModelPalette).open(
            self._model_selection.options,
            self.runtime_info.provider,
            self.runtime_info.model,
        )

    def _switch_model(self, selection: ModelOption) -> None:
        """先完整创建 Provider，再更新 Session 与界面状态快照。"""

        if self.chat_session is None:
            self._write_message("System", "模型切换不可用。")
            return
        if self._model_selection is None:
            self._write_message("System", "当前没有可选模型。")
            return
        try:
            result = self._model_selection.switch(self.chat_session, selection)
        except ModelSelectionError as exc:
            self._write_message("System", exc.user_message)
            return
        self._health = self._health.without_code("configuration")
        self.runtime_info = result.runtime_info
        self.run_status = self._idle_status()
        self._update_status_bar()
        self._write_message(
            "System",
            (
                "已切换模型："
                f"{provider_display_name(result.runtime_info.provider)} · "
                f"{result.runtime_info.model}"
            ),
        )
        if result.warning is not None:
            self._write_message("System", result.warning)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """将输入变化交给命令与技能候选组件。"""

        if event.text_area.id == "prompt":
            self.query_one(CommandPalette).filter_input(
                event.text_area.text, enabled=not self._request.is_active
            )
            self._refresh_skill_palette(event.text_area)

    def on_text_area_selection_changed(
        self,
        event: TextArea.SelectionChanged,
    ) -> None:
        """光标移动后重新定位所在的技能引用片段。"""

        if event.text_area.id == "prompt":
            self._refresh_skill_palette(event.text_area)

    def _refresh_skill_palette(self, prompt: TextArea) -> None:
        """用当前运行时摘要刷新技能候选，不触发磁盘扫描。"""

        skills = ()
        if (
            self._skill_runtime is not None
            and self._issue_message("skills") is None
        ):
            skills = self._skill_runtime.available_skills()
        selection = prompt.selection
        self.query_one(SkillPalette).filter_input(
            prompt.text,
            prompt.cursor_location,
            skills,
            enabled=(
                not self._request.is_active
                and selection.start == selection.end
            ),
        )

    def action_complete_suggestion(self) -> None:
        """补全命令或 Skill 候选，实际执行留给下一次提交。"""

        if isinstance(self.screen, ToolApprovalScreen):
            self.screen.focus_next()
            return
        command = self.query_one(CommandPalette).take_selection()
        if command is not None:
            prompt = self.query_one("#prompt", TextArea)
            prompt.load_text(command)
            prompt.move_cursor(prompt.document.end)
            return
        completion = self.query_one(SkillPalette).take_selection()
        if completion is not None:
            prompt = self.query_one("#prompt", TextArea)
            prompt.replace(
                completion.text,
                completion.start,
                completion.end,
                maintain_selection_offset=False,
            )
            prompt.move_cursor(
                (
                    completion.start[0],
                    completion.start[1] + len(completion.text),
                )
            )
            return
        self.screen.focus_next()

    def _refresh_skill_status(self) -> None:
        """安装完成后只读取进程内状态，不扫描项目 Skill 目录。"""

        if self._skill_runtime is None:
            return
        status = self._skill_runtime.status()
        self._skills_count = status.skills_count
        self._health = self._health.without_code("skills")
        if status.error is not None:
            self._health = self._health.replacing(
                StartupIssue(
                    "skills",
                    status.error,
                    IssueSeverity.ERROR,
                    blocks_prompt=False,
                )
            )
        self._update_status_bar()
        if self.is_mounted:
            self._refresh_skill_palette(self.query_one("#prompt", TextArea))

    def action_previous_input(self) -> None:
        """向更早的已发送输入移动，并在首次浏览时保存当前草稿。"""

        palette = self.query_one(CommandPalette)
        if palette.is_open:
            palette.move_selection(-1)
            return
        model_palette = self.query_one(ModelPalette)
        if model_palette.is_open:
            model_palette.move_selection(-1)
            return
        skill_palette = self.query_one(SkillPalette)
        if skill_palette.is_open:
            skill_palette.move_selection(-1)
            return
        previous = self._input_history.previous(self.query_one("#prompt", TextArea).text)
        if previous is not None:
            self._load_history_input(previous)

    def action_next_input(self) -> None:
        """向更新的输入移动，并在越过末项时恢复浏览前草稿。"""

        palette = self.query_one(CommandPalette)
        if palette.is_open:
            palette.move_selection(1)
            return
        model_palette = self.query_one(ModelPalette)
        if model_palette.is_open:
            model_palette.move_selection(1)
            return
        skill_palette = self.query_one(SkillPalette)
        if skill_palette.is_open:
            skill_palette.move_selection(1)
            return
        following = self._input_history.next()
        if following is not None:
            self._load_history_input(following)

    def _load_history_input(self, input_text: str) -> None:
        """加载历史原文，并把光标放到多行文档末尾便于继续编辑。"""

        prompt = self.query_one("#prompt", TextArea)
        prompt.load_text(input_text)
        prompt.move_cursor(prompt.document.end)

    def action_confirm_exit(self) -> None:
        """优先清空输入；输入为空时才进入取消请求和双 Esc 退出。"""

        if isinstance(self.screen, ToolApprovalScreen):
            # App 的高优先级 Esc 会先收到按键；审批界面必须把它收敛为拒绝。
            self.screen.dismiss(False)
            return
        prompt = self.query_one("#prompt", TextArea)
        model_palette = self.query_one(ModelPalette)
        if model_palette.is_open:
            model_palette.dismiss()
            self._last_escape_at = None
            prompt.focus()
            return
        palette = self.query_one(CommandPalette)
        if palette.is_open:
            palette.dismiss(prompt.text)
            self._last_escape_at = None
            return
        skill_palette = self.query_one(SkillPalette)
        if skill_palette.is_open:
            skill_palette.dismiss(prompt.text, prompt.cursor_location)
            self._last_escape_at = None
            return
        if prompt.text:
            prompt.load_text("")
            self._input_history.reset_navigation()
            self._last_escape_at = None
            prompt.focus()
            return

        now = self.clock()
        if self._last_escape_at is not None:
            elapsed = now - self._last_escape_at
            if 0 <= elapsed <= self.ESCAPE_CONFIRM_SECONDS:
                self._last_escape_at = None
                self._request.cancel(show_message=False)
                self.exit()
                return

        self._last_escape_at = now
        self._request.cancel(show_message=False)
        self._write_message("System", "再次按 Esc 退出")
