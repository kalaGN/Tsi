"""Textual 单轮对话界面、状态切换与请求取消逻辑。"""

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import Footer, RichLog, Static, TextArea
from textual.worker import Worker, get_current_worker

from app.runtime.chat import (
    CHAT_MODEL,
    CHAT_PROVIDER,
    ChatResult,
    ChatRuntimeError,
    run_chat,
)
from app.tui.state import RunStatus


ChatRunner = Callable[[str], Awaitable[ChatResult]]


def format_response_body(body: Any) -> str:
    """从兼容模式响应中提取正文，未知结构降级为可读 JSON。"""

    if isinstance(body, dict):
        output_text = body.get("output_text")
        if isinstance(output_text, str) and output_text:
            return output_text

        output = body.get("output")
        if isinstance(output, list):
            direct_fragments = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                item_text = item.get("text")
                if isinstance(item_text, str) and item_text:
                    direct_fragments.append(item_text)
            if direct_fragments:
                return "\n".join(direct_fragments)

            nested_fragments = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if isinstance(content, list):
                    for content_item in content:
                        if not isinstance(content_item, dict):
                            continue
                        content_text = content_item.get("text")
                        if isinstance(content_text, str) and content_text:
                            nested_fragments.append(content_text)
            if nested_fragments:
                return "\n".join(nested_fragments)

    return json.dumps(body, ensure_ascii=False, indent=2, default=str)


class ChatTuiApp(App[None]):
    """复用 Chat Runtime 的本地全屏单轮对话界面。"""

    TITLE = "FastAPI Agent TUI"
    BINDINGS = [
        Binding("enter", "submit_prompt", "Send", priority=True),
        Binding("escape", "exit_app", "Exit", priority=True),
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
        api_key_configured: bool | None = None,
    ) -> None:
        super().__init__()
        self.chat_runner = chat_runner
        self.api_key_configured = (
            self._has_api_key()
            if api_key_configured is None
            else api_key_configured
        )
        self._active_worker: Worker[None] | None = None
        self._request_generation = 0

    @staticmethod
    def _has_api_key() -> bool:
        api_key = os.getenv("DASHSCOPE_API_KEY")
        return bool(api_key and api_key.strip())

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
            placeholder="Type a message. Enter: send, Esc: exit",
        )
        yield Static(id="status-bar", markup=False)
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        self.query_one("#prompt", TextArea).focus()
        if not self.api_key_configured:
            self.run_status = RunStatus.ERROR
            self._write_message("Error", "Upstream API key is not configured")
        self._update_status_bar()

    def watch_run_status(self) -> None:
        if self.is_mounted:
            self._update_status_bar()

    def _update_status_bar(self) -> None:
        key_status = "configured" if self.api_key_configured else "missing"
        self.query_one("#status-bar", Static).update(
            f"{CHAT_PROVIDER} | {CHAT_MODEL} | "
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

        prompt_widget.load_text("")
        self._write_message("You", input_text)
        self.run_status = RunStatus.THINKING
        self._request_generation += 1
        generation = self._request_generation
        self._active_worker = self.run_worker(
            self._run_prompt(input_text, generation),
            name="chat-request",
            group="chat",
            exclusive=True,
            exit_on_error=False,
        )

    async def _run_prompt(self, input_text: str, generation: int) -> None:
        worker = get_current_worker()
        try:
            result = await self.chat_runner(input_text)
            # 取消会递增代次；即使底层协程晚返回，也不能写回陈旧结果。
            if worker.is_cancelled or generation != self._request_generation:
                return
            self._write_message("Assistant", format_response_body(result.body))
            self.run_status = RunStatus.READY
        except ChatRuntimeError as exc:
            if worker.is_cancelled or generation != self._request_generation:
                return
            self._write_message("Error", exc.user_message)
            self.run_status = RunStatus.ERROR
        except Exception:
            if worker.is_cancelled or generation != self._request_generation:
                return
            # 未知异常只在界面显示中立文案，避免泄露堆栈或敏感上下文。
            self._write_message("Error", "Unexpected internal error")
            self.run_status = RunStatus.ERROR
        finally:
            if generation == self._request_generation:
                self._active_worker = None
                self.query_one("#prompt", TextArea).focus()

    def action_exit_app(self) -> None:
        self._cancel_active_request(show_message=False)
        self.exit()

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
