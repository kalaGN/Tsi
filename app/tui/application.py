"""Textual 单轮对话界面、状态切换与请求取消逻辑。"""

import time
from collections.abc import Awaitable, Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import Footer, RichLog, Static, TextArea
from textual.worker import Worker, get_current_worker

from app.runtime.chat import (
    ChatResult,
    ChatRuntimeError,
    ChatRuntimeInfo,
    get_chat_runtime_info,
    run_chat,
)
from app.tui.state import RunStatus


ChatRunner = Callable[[str], Awaitable[ChatResult]]
Clock = Callable[[], float]


class ChatTuiApp(App[None]):
    """复用 Chat Runtime 的本地全屏单轮对话界面。"""

    TITLE = "FastAPI Agent TUI"
    ESCAPE_CONFIRM_SECONDS = 1.5
    BINDINGS = [
        Binding("enter", "submit_prompt", "Send", priority=True),
        Binding("escape", "confirm_exit", "Exit (x2)", priority=True),
    ]
    CSS = """
    Screen {
        layout: vertical;
    }

    #title {
        height: 3;
        padding: 1 2;
        text-style: bold;
        background: $primary;
        color: $text;
    }

    #transcript {
        height: 1fr;
        padding: 1 2;
        border: solid $primary-background;
    }

    #prompt {
        height: 7;
        min-height: 4;
        border: solid $accent;
    }

    #status-bar {
        height: 1;
        padding: 0 1;
        background: $panel;
    }
    """

    run_status = reactive(RunStatus.READY)

    def __init__(
        self,
        chat_runner: ChatRunner = run_chat,
        runtime_info: ChatRuntimeInfo | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        super().__init__()
        self.chat_runner = chat_runner
        self.clock = clock
        self._configuration_error: str | None = None
        if runtime_info is None:
            try:
                runtime_info = get_chat_runtime_info()
            except ChatRuntimeError as exc:
                # 配置错误不能阻止 TUI 构造，挂载后以安全状态提示用户。
                self._configuration_error = exc.user_message
                runtime_info = ChatRuntimeInfo("unknown", "-", False)
        self.runtime_info = runtime_info
        self._active_worker: Worker[None] | None = None
        self._request_generation = 0
        self._last_escape_at: float | None = None

    def compose(self) -> ComposeResult:
        yield Static(self.TITLE, id="title", markup=False)
        yield RichLog(
            id="transcript",
            wrap=True,
            markup=False,
            auto_scroll=True,
        )
        yield TextArea(
            id="prompt",
            soft_wrap=True,
            placeholder="Type a message. Enter: send, Esc x2: exit",
        )
        yield Static(id="status-bar", markup=False)
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        self.query_one("#prompt", TextArea).focus()
        if self._configuration_error is not None:
            self.run_status = RunStatus.ERROR
            self._write_message("Error", self._configuration_error)
        elif not self.runtime_info.api_key_configured:
            self.run_status = RunStatus.ERROR
            self._write_message("Error", "Upstream API key is not configured")
        self._update_status_bar()

    def watch_run_status(self) -> None:
        if self.is_mounted:
            self._update_status_bar()

    def _update_status_bar(self) -> None:
        key_status = (
            "configured" if self.runtime_info.api_key_configured else "missing"
        )
        self.query_one("#status-bar", Static).update(
            f"{_provider_display_name(self.runtime_info.provider)} | "
            f"{self.runtime_info.model} | "
            f"Key: {key_status} | {self.run_status.value}"
        )

    def _write_message(self, role: str, content: str) -> None:
        message = Text()
        message.append(role, style="bold")
        message.append("\n")
        message.append(content)
        self.query_one("#transcript", RichLog).write(message)

    def action_submit_prompt(self) -> None:
        prompt_widget = self.query_one("#prompt", TextArea)
        input_text = prompt_widget.text
        command = input_text.strip()

        if command == "/quit":
            self._cancel_active_request(show_message=False)
            self.exit()
            return

        if self._active_worker is not None:
            return

        if command == "/clear":
            prompt_widget.load_text("")
            self.query_one("#transcript", RichLog).clear()
            self.run_status = RunStatus.READY
            return

        if not input_text.strip():
            self._write_message("System", "Input must not be blank")
            return

        started_at = self.clock()
        prompt_widget.load_text("")
        self._write_message("You", input_text)
        self.run_status = RunStatus.THINKING
        self._request_generation += 1
        generation = self._request_generation
        self._active_worker = self.run_worker(
            self._run_prompt(input_text, generation, started_at),
            name="chat-request",
            group="chat",
            exclusive=True,
            exit_on_error=False,
        )

    async def _run_prompt(
        self,
        input_text: str,
        generation: int,
        started_at: float,
    ) -> None:
        worker = get_current_worker()
        try:
            result = await self.chat_runner(input_text)
            # 取消会递增代次；即使底层协程晚返回，也不能写回陈旧结果。
            if worker.is_cancelled or generation != self._request_generation:
                return
            self._write_message("Assistant", result.output_text)
            self._write_elapsed_time(started_at)
            self.run_status = RunStatus.READY
        except ChatRuntimeError as exc:
            if worker.is_cancelled or generation != self._request_generation:
                return
            self._write_message("Error", exc.user_message)
            self._write_elapsed_time(started_at)
            self.run_status = RunStatus.ERROR
        except Exception:
            if worker.is_cancelled or generation != self._request_generation:
                return
            # 未知异常只在界面显示中立文案，避免泄露堆栈或敏感上下文。
            self._write_message("Error", "Unexpected internal error")
            self._write_elapsed_time(started_at)
            self.run_status = RunStatus.ERROR
        finally:
            if generation == self._request_generation:
                self._active_worker = None
                self.query_one("#prompt", TextArea).focus()

    def _write_elapsed_time(self, started_at: float) -> None:
        """在请求结果后显示不受系统时间调整影响的单轮耗时。"""

        elapsed = self.clock() - started_at
        self._write_message("System", f"耗时：{elapsed:.2f} 秒")

    def action_confirm_exit(self) -> None:
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
        worker = self._active_worker
        if worker is None:
            return

        self._request_generation += 1
        self._active_worker = None
        worker.cancel()
        self.run_status = RunStatus.READY
        if show_message:
            self._write_message("System", "Request cancelled")
        self.query_one("#prompt", TextArea).focus()


def _provider_display_name(provider: str) -> str:
    """保留 DeepSeek 品牌大小写，其余名称使用常规标题格式。"""

    if provider == "deepseek":
        return "DeepSeek"
    return provider.title()
