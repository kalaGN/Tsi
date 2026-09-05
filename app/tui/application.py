"""Textual 多轮对话界面、状态切换与请求取消逻辑。"""

import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Button, RichLog, Static, TextArea
from textual.worker import Worker, get_current_worker

from app.runtime.chat import (
    ChatResult,
    ChatRuntimeError,
    ChatRuntimeInfo,
    get_chat_runtime_info,
)
from app.runtime.session import ChatSession
from app.runtime.session_store import SessionStore
from app.services.llm.contracts import (
    ChatRole,
    TextDeltaHandler,
    TextResetHandler,
)
from app.runtime.tool_loop import (
    DEFAULT_TOOL_LOOP_LIMITS,
    WORKSPACE_TOOL_LOOP_LIMITS,
)
from app.tui.activity_bar import ActivityBar
from app.tui.approval import ToolApprovalScreen
from app.tui.command_palette import CommandPalette
from app.tui.commands import LocalCommand, parse_local_command
from app.tui.input_history import InputHistory
from app.tui.state import RunStatus
from app.tui.status_bar import StatusBar, StatusBarState
from app.tui.transcript import StreamOutput, Transcript
from app.tui.widgets import PromptTextArea
from app.tui.workspace_changes import AppliedChangeTracker
from tools import AnyToolApprovalRequest, ToolRegistry

if TYPE_CHECKING:
    from app.runtime.skill_runtime import SkillRuntime


class ChatRunner(Protocol):
    """TUI 内部可注入的流式对话调用契约。"""

    def __call__(
        self,
        input_text: str,
        *,
        on_text_delta: TextDeltaHandler | None = None,
        on_text_reset: TextResetHandler | None = None,
        on_tool_approval=None,
        on_tool_result=None,
    ) -> Awaitable[ChatResult]:
        ...


Clock = Callable[[], float]


class ChatTuiApp(App[None]):
    """复用 Chat Runtime 的本地全屏多轮对话界面。"""

    TITLE = "Tsi 助手"
    ESCAPE_CONFIRM_SECONDS = 1.5
    ACTIVITY_INTERVAL_SECONDS = 0.1
    BINDINGS = [
        Binding("tab", "complete_command", "Complete", show=False, priority=True),
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
        chat_runner: ChatRunner | None = None,
        runtime_info: ChatRuntimeInfo | None = None,
        clock: Clock = time.monotonic,
        chat_session: ChatSession | None = None,
        system_prompt: str | None = None,
        system_prompt_error: str | None = None,
        workspace_registry: ToolRegistry | None = None,
        workspace_error: str | None = None,
        skills_count: int = 0,
        skills_error: str | None = None,
        skill_runtime: "SkillRuntime | None" = None,
    ) -> None:
        """初始化运行时信息、可恢复会话以及可注入的测试边界。"""

        super().__init__()
        self.clock = clock
        self._configuration_error: str | None = None
        self._history_error: str | None = None
        self._system_prompt_error = system_prompt_error
        self._workspace_error = workspace_error
        self._skill_runtime = skill_runtime
        self._workspace_enabled = (
            workspace_registry is not None or skill_runtime is not None
        )
        if skill_runtime is not None:
            skill_status = skill_runtime.status()
            skills_count = skill_status.skills_count
            skills_error = skill_status.error
        self._skills_count = skills_count
        self._skills_error = skills_error
        if runtime_info is None:
            try:
                runtime_info = get_chat_runtime_info()
            except ChatRuntimeError as exc:
                # 配置错误不能阻止 TUI 构造，挂载后以安全状态提示用户。
                self._configuration_error = exc.user_message
                runtime_info = ChatRuntimeInfo("unknown", "-", False)
        self.runtime_info = runtime_info
        if chat_session is None and chat_runner is None:
            store = SessionStore()
            try:
                chat_session = ChatSession.load(
                    store,
                    system_prompt=system_prompt,
                    registry=workspace_registry,
                    execution_snapshot_provider=(
                        skill_runtime.snapshot if skill_runtime is not None else None
                    ),
                    tool_loop_limits=(
                        WORKSPACE_TOOL_LOOP_LIMITS
                        if workspace_registry is not None
                        else DEFAULT_TOOL_LOOP_LIMITS
                    ),
                )
            except ChatRuntimeError as exc:
                # 保留损坏文件，只允许用户通过 /clear 显式删除。
                self._history_error = exc.user_message
                chat_session = ChatSession(
                    store,
                    system_prompt=system_prompt,
                    registry=workspace_registry,
                    execution_snapshot_provider=(
                        skill_runtime.snapshot if skill_runtime is not None else None
                    ),
                    tool_loop_limits=(
                        WORKSPACE_TOOL_LOOP_LIMITS
                        if workspace_registry is not None
                        else DEFAULT_TOOL_LOOP_LIMITS
                    ),
                )
        self.chat_session = chat_session
        self._system_prompt_loaded = (
            chat_session.system_prompt_loaded
            if chat_session is not None
            else bool(system_prompt)
        )
        self.chat_runner = (
            chat_session.send if chat_runner is None and chat_session else chat_runner
        )
        if self.chat_runner is None:
            raise ValueError("chat_runner or chat_session is required")
        self._active_worker: Worker[None] | None = None
        self._request_generation = 0
        self._last_escape_at: float | None = None
        self._activity_timer: Timer | None = None
        self._activity_started_at: float | None = None
        self._activity_generation: int | None = None
        self._stream_generation: int | None = None
        self._input_history = InputHistory(
            [
                message.content
                for message in chat_session.messages
                if message.role is ChatRole.USER
            ]
            if chat_session is not None
            else []
        )

    def compose(self) -> ComposeResult:
        """声明标题、对话记录、输入框和状态栏布局。"""

        yield Static(self.TITLE, id="title", markup=False)
        yield Transcript()
        yield StreamOutput()
        yield ActivityBar()
        yield CommandPalette()
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
        if self._configuration_error is not None:
            self.run_status = RunStatus.ERROR
            self._write_message("Error", self._configuration_error)
        elif not self.runtime_info.api_key_configured:
            self.run_status = RunStatus.ERROR
            self._write_message("Error", "Upstream API key is not configured")
        if self._history_error is not None:
            self.run_status = RunStatus.ERROR
            self._write_message("Error", self._history_error)
            self._write_message("System", "Use /clear to reset saved conversation")
        if self._system_prompt_error is not None:
            self.run_status = RunStatus.ERROR
            self._write_message("Error", self._system_prompt_error)
        if self._workspace_error is not None:
            self.run_status = RunStatus.ERROR
            self._write_message("Error", self._workspace_error)
        if self._skills_error is not None:
            self.run_status = RunStatus.ERROR
            self._write_message("Error", self._skills_error)
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
                system_prompt_error=self._system_prompt_error,
                workspace_enabled=self._workspace_enabled,
                workspace_error=self._workspace_error,
                skills_count=self._skills_count,
                skills_error=self._skills_error,
                run_status=self.run_status,
            )
        )

    def _write_message(self, role: str, content: str) -> None:
        """将消息交给展示组件，应用只协调消息产生的时机。"""

        self.query_one(Transcript).write_message(role, content)

    def action_submit_prompt(self) -> None:
        """处理本地命令、输入校验，并启动唯一的异步对话请求。"""

        if isinstance(self.screen, ToolApprovalScreen):
            self._submit_focused_approval_action()
            return
        prompt_widget = self.query_one("#prompt", TextArea)
        if self.query_one(CommandPalette).is_open:
            self.action_complete_command()
            return
        input_text = prompt_widget.text
        command = parse_local_command(input_text)
        if command is LocalCommand.QUIT:
            self._cancel_active_request(show_message=False)
            self.exit()
            return
        if self._active_worker is not None:
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
        if command is LocalCommand.SKILLS:
            prompt.load_text("")
            self._write_available_skills()
            return True
        return False

    def _clear_conversation(self, prompt: TextArea) -> None:
        """先清理持久化会话，成功后再重置界面与内存历史。"""

        try:
            if self.chat_session is not None:
                self.chat_session.clear()
        except ChatRuntimeError as exc:
            self._write_message("Error", exc.user_message)
            self.run_status = RunStatus.ERROR
            return
        self._history_error = None
        self._input_history.clear()
        self._finish_stream_output()
        prompt.load_text("")
        self.query_one("#transcript", RichLog).clear()
        self.run_status = (
            RunStatus.ERROR
            if self._system_prompt_error is not None
            else RunStatus.READY
        )

    def _can_start_prompt(self, input_text: str) -> bool:
        """按既有优先级展示阻断原因，避免启动无效请求。"""

        if self._history_error is not None:
            self._write_message("Error", self._history_error)
            self._write_message("System", "Use /clear to reset saved conversation")
            return False

        if self._system_prompt_error is not None:
            self._write_message("Error", self._system_prompt_error)
            self.run_status = RunStatus.ERROR
            return False

        if self._workspace_error is not None:
            self._write_message("Error", self._workspace_error)
            self.run_status = RunStatus.ERROR
            return False

        if not input_text.strip():
            self._write_message("System", "Input must not be blank")
            return False
        return True

    def _start_prompt_request(self, input_text: str, prompt: TextArea) -> None:
        """提交输入并创建该请求唯一的 Worker、流缓冲与活动计时。"""

        started_at = self.clock()
        # 新请求重新开始双 Esc 手势，避免上一次取消被误判为本次的退出确认。
        self._last_escape_at = None
        self._input_history.append(input_text)
        prompt.load_text("")
        self._write_message("You", input_text)
        self.run_status = RunStatus.THINKING
        self._request_generation += 1
        generation = self._request_generation
        self._begin_stream_output(generation)
        self._active_worker = self.run_worker(
            self._run_prompt(input_text, generation, started_at),
            name="chat-request",
            group="chat",
            exclusive=True,
            exit_on_error=False,
        )
        self._start_activity(started_at, generation)

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

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """将输入变化交给命令组件，应用只提供请求是否空闲。"""

        if event.text_area.id == "prompt":
            self.query_one(CommandPalette).filter_input(
                event.text_area.text, enabled=self._active_worker is None
            )

    def action_complete_command(self) -> None:
        """应用候选补全文本，命令执行留给下一次提交。"""

        if isinstance(self.screen, ToolApprovalScreen):
            self.screen.focus_next()
            return
        command = self.query_one(CommandPalette).take_selection()
        if command is None:
            self.screen.focus_next()
            return
        prompt = self.query_one("#prompt", TextArea)
        prompt.load_text(command)
        prompt.move_cursor(prompt.document.end)

    async def _run_prompt(
        self,
        input_text: str,
        generation: int,
        started_at: float,
    ) -> None:
        """执行模型请求，并用请求代次阻止取消后的陈旧结果写回。"""

        worker = get_current_worker()
        applied_changes = AppliedChangeTracker()

        try:
            runner_arguments = {
                "on_text_delta": lambda delta: self._append_stream_delta(
                    delta,
                    generation,
                ),
                "on_text_reset": lambda: self._reset_stream_step(generation),
            }
            if self._workspace_enabled:
                runner_arguments["on_tool_approval"] = (
                    lambda request: self._approve_tool(request, generation)
                )
                runner_arguments["on_tool_result"] = applied_changes.observe
            result = await self.chat_runner(input_text, **runner_arguments)
            # 取消会递增代次；即使底层协程晚返回，也不能写回陈旧结果。
            if worker.is_cancelled or generation != self._request_generation:
                return
            self._flush_stream_output(generation)
            self._finish_stream_output(generation)
            self._write_message("Assistant", result.output_text)
            self._write_elapsed_time(started_at)
            self.run_status = RunStatus.READY
        except ChatRuntimeError as exc:
            if worker.is_cancelled or generation != self._request_generation:
                return
            self._write_message("Error", exc.user_message)
            self._write_applied_change_warning(applied_changes.paths())
            self._write_elapsed_time(started_at)
            self.run_status = RunStatus.ERROR
        except Exception:
            if worker.is_cancelled or generation != self._request_generation:
                return
            # 未知异常只在界面显示中立文案，避免泄露堆栈或敏感上下文。
            self._write_message("Error", "Unexpected internal error")
            self._write_applied_change_warning(applied_changes.paths())
            self._write_elapsed_time(started_at)
            self.run_status = RunStatus.ERROR
        finally:
            if generation == self._request_generation:
                self._refresh_skill_status()
                self._finish_stream_output(generation)
                self._stop_activity(generation)
                self._active_worker = None
                self.query_one("#prompt", TextArea).focus()

    def _refresh_skill_status(self) -> None:
        """安装完成后只读取进程内状态，不扫描项目 Skill 目录。"""

        if self._skill_runtime is None:
            return
        status = self._skill_runtime.status()
        self._skills_count = status.skills_count
        self._skills_error = status.error
        self._update_status_bar()

    def _start_activity(self, started_at: float, generation: int) -> None:
        """为当前请求创建实时思考提示和专属周期 Timer。"""

        self._stop_activity()
        self._activity_started_at = started_at
        self._activity_generation = generation
        self.query_one(ActivityBar).show_activity(0.0, self.run_status)
        self._activity_timer = self.set_interval(
            self.ACTIVITY_INTERVAL_SECONDS,
            lambda: self._refresh_activity(generation),
            name="request-activity",
        )

    def action_previous_input(self) -> None:
        """向更早的已发送输入移动，并在首次浏览时保存当前草稿。"""

        palette = self.query_one(CommandPalette)
        if palette.is_open:
            palette.move_selection(-1)
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
        following = self._input_history.next()
        if following is not None:
            self._load_history_input(following)

    def _load_history_input(self, input_text: str) -> None:
        """加载历史原文，并把光标放到多行文档末尾便于继续编辑。"""

        prompt = self.query_one("#prompt", TextArea)
        prompt.load_text(input_text)
        prompt.move_cursor(prompt.document.end)

    def _refresh_activity(self, generation: int) -> None:
        """按单调时钟刷新当前请求的动画帧和已等待时间。"""

        started_at = self._activity_started_at
        if (
            started_at is None
            or generation != self._activity_generation
            or generation != self._request_generation
        ):
            return
        elapsed = max(0.0, self.clock() - started_at)
        self.query_one(ActivityBar).show_activity(elapsed, self.run_status, advance=True)
        self._flush_stream_output(generation)

    def _begin_stream_output(self, generation: int) -> None:
        """为新请求初始化独立的临时文本缓冲和请求代次。"""

        self._finish_stream_output()
        self._stream_generation = generation

    def _append_stream_delta(self, delta: str, generation: int) -> None:
        """仅为当前请求累计非空文本，等待活动 Timer 合并绘制。"""

        if (
            not isinstance(delta, str)
            or not delta
            or generation != self._stream_generation
            or generation != self._request_generation
        ):
            return
        self.query_one(StreamOutput).append_delta(delta)

    def _flush_stream_output(self, generation: int) -> None:
        """把当前步骤完整纯文本批量写入可滚动临时区域。"""

        if generation == self._stream_generation:
            self.query_one(StreamOutput).flush()

    def _reset_stream_step(self, generation: int) -> None:
        """撤销工具中间步骤的临时文本，并允许同一请求继续输出。"""

        if generation != self._stream_generation:
            return
        if self.is_mounted:
            self.query_one(StreamOutput).reset_output()

    def _finish_stream_output(self, expected_generation: int | None = None) -> None:
        """清空匹配请求的临时内容，并结束其后续 Delta 接收。"""

        if (
            expected_generation is not None
            and expected_generation != self._stream_generation
        ):
            return
        self._stream_generation = None
        if self.is_mounted:
            self.query_one(StreamOutput).reset_output()

    async def _approve_tool(
        self,
        request: AnyToolApprovalRequest,
        generation: int,
    ) -> bool:
        """在当前请求 Worker 中等待 Modal，并拒绝取消后的陈旧审批。"""

        if generation != self._request_generation:
            return False
        self.run_status = RunStatus.AWAITING_APPROVAL
        approved = await self.push_screen_wait(ToolApprovalScreen(request))
        if generation != self._request_generation:
            return False
        self.run_status = RunStatus.THINKING
        return approved is True

    def _write_applied_change_warning(
        self,
        paths: tuple[str, ...],
    ) -> None:
        """模型未完成时明确展示仍保留在磁盘上的相对路径。"""

        if paths:
            self._write_message(
                "System",
                "本轮已写入但尚未完成：" + "、".join(paths),
            )

    def _stop_activity(self, expected_generation: int | None = None) -> None:
        """停止匹配请求的 Timer，并清空全部活动展示状态。"""

        if (
            expected_generation is not None
            and self._activity_generation != expected_generation
        ):
            return
        timer = self._activity_timer
        if timer is not None:
            timer.stop()
        self._activity_timer = None
        self._activity_started_at = None
        self._activity_generation = None
        if self.is_mounted:
            self.query_one(ActivityBar).reset_activity()

    def _write_elapsed_time(self, started_at: float) -> None:
        """在请求结果后显示不受系统时间调整影响的单轮耗时。"""

        elapsed = self.clock() - started_at
        self._write_message("System", f"耗时：{elapsed:.2f} 秒")

    def action_confirm_exit(self) -> None:
        """优先清空输入；输入为空时才进入取消请求和双 Esc 退出。"""

        if isinstance(self.screen, ToolApprovalScreen):
            # App 的高优先级 Esc 会先收到按键；审批界面必须把它收敛为拒绝。
            self.screen.dismiss(False)
            return
        prompt = self.query_one("#prompt", TextArea)
        palette = self.query_one(CommandPalette)
        if palette.is_open:
            palette.dismiss(prompt.text)
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
                self._cancel_active_request(show_message=False)
                self.exit()
                return

        self._last_escape_at = now
        self._cancel_active_request(show_message=False)
        self._write_message("System", "再次按 Esc 退出")

    def _cancel_active_request(self, show_message: bool) -> None:
        """取消当前 Worker，并递增代次使其可能的延迟结果失效。"""

        worker = self._active_worker
        if worker is None:
            self._finish_stream_output()
            self._stop_activity()
            return

        generation = self._request_generation
        self._request_generation += 1
        self._active_worker = None
        self._finish_stream_output(generation)
        self._stop_activity(generation)
        worker.cancel()
        self.run_status = RunStatus.READY
        if show_message:
            self._write_message("System", "Request cancelled")
        self.query_one("#prompt", TextArea).focus()
