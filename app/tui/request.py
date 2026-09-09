"""TUI 单次请求的可注入调用契约与生命周期协调。"""

import time
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from textual.timer import Timer
from textual.worker import Worker, get_current_worker

from app.runtime.chat import ChatResult, ChatRuntimeError
from app.services.llm.contracts import (
    TextDeltaHandler,
    TextResetHandler,
    TokenUsage,
)
from app.tui.activity_bar import ActivityBar
from app.tui.approval import ToolApprovalScreen
from app.tui.state import RunStatus
from app.tui.transcript import StreamOutput
from app.tui.workspace_changes import AppliedChangeTracker
from tools import AnyToolApprovalRequest


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
DEFAULT_CLOCK: Clock = time.monotonic

WidgetType = TypeVar("WidgetType")


class RequestHost(Protocol):
    """RequestCoordinator 需要的最小 Textual 宿主能力。"""

    run_status: RunStatus
    is_mounted: bool

    def run_worker(self, work, **kwargs) -> Worker[None]:
        ...

    def set_interval(self, interval: float, callback, **kwargs) -> Timer:
        ...

    def query_one(self, expect_type: type[WidgetType]) -> WidgetType:
        ...

    def write_request_message(self, role: str, content: str) -> None:
        ...

    async def push_screen_wait(self, screen: ToolApprovalScreen) -> bool:
        ...

    def refresh_request_skill_status(self) -> None:
        ...

    def focus_request_prompt(self) -> None:
        ...


class RequestCoordinator:
    """独占 TUI 请求 Worker、Timer、流输出和取消代次。"""

    def __init__(
        self,
        host: RequestHost,
        runner: ChatRunner,
        *,
        clock: Clock = DEFAULT_CLOCK,
        workspace_enabled: bool = False,
        activity_interval_seconds: float = 0.1,
    ) -> None:
        self._host = host
        self._runner = runner
        self._clock = clock
        self._workspace_enabled = workspace_enabled
        self._activity_interval_seconds = activity_interval_seconds
        self._active_worker: Worker[None] | None = None
        self._generation = 0
        self._activity_timer: Timer | None = None
        self._activity_started_at: float | None = None
        self._activity_generation: int | None = None
        self._stream_generation: int | None = None

    @property
    def is_active(self) -> bool:
        return self._active_worker is not None

    @property
    def active_worker(self) -> Worker[None] | None:
        return self._active_worker

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def activity_timer(self) -> Timer | None:
        return self._activity_timer

    @property
    def activity_generation(self) -> int | None:
        return self._activity_generation

    def start(self, input_text: str) -> None:
        """创建当前请求唯一的 Worker、流缓冲和活动 Timer。"""

        if self.is_active:
            return
        started_at = self._clock()
        self._host.write_request_message("You", input_text)
        self._host.run_status = RunStatus.THINKING
        self._generation += 1
        generation = self._generation
        self._begin_stream(generation)
        self._active_worker = self._host.run_worker(
            self._run(input_text, generation, started_at),
            name="chat-request",
            group="chat",
            exclusive=True,
            exit_on_error=False,
        )
        self._start_activity(started_at, generation)

    def cancel(self, *, show_message: bool) -> None:
        """使当前代次先失效，再停止展示并取消 Worker。"""

        worker = self._active_worker
        if worker is None:
            self.finish_stream()
            self._stop_activity()
            return

        generation = self._generation
        self._generation += 1
        self._active_worker = None
        self.finish_stream(generation)
        self._stop_activity(generation)
        worker.cancel()
        self._host.run_status = RunStatus.READY
        if show_message:
            self._host.write_request_message("System", "Request cancelled")
        self._host.focus_request_prompt()

    def clear_transient_output(self) -> None:
        self.finish_stream()
        self._stop_activity()

    def refresh_activity(self, generation: int) -> None:
        """刷新匹配请求的活动展示并合并绘制流文本。"""

        started_at = self._activity_started_at
        if (
            started_at is None
            or generation != self._activity_generation
            or generation != self._generation
        ):
            return
        elapsed = max(0.0, self._clock() - started_at)
        self._host.query_one(ActivityBar).show_activity(
            elapsed,
            self._host.run_status,
            advance=True,
        )
        self._flush_stream(generation)

    async def _run(
        self,
        input_text: str,
        generation: int,
        started_at: float,
    ) -> None:
        worker = get_current_worker()
        applied_changes = AppliedChangeTracker()

        try:
            runner_arguments = {
                "on_text_delta": lambda delta: self._append_stream_delta(
                    delta,
                    generation,
                ),
                "on_text_reset": lambda: self._reset_stream(generation),
            }
            if self._workspace_enabled:
                runner_arguments["on_tool_approval"] = (
                    lambda request: self._approve_tool(request, generation)
                )
                runner_arguments["on_tool_result"] = applied_changes.observe
            result = await self._runner(input_text, **runner_arguments)
            if worker.is_cancelled or generation != self._generation:
                return
            self._flush_stream(generation)
            self.finish_stream(generation)
            self._host.write_request_message("Assistant", result.output_text)
            self._write_request_statistics(started_at, result.token_usage)
            self._host.run_status = RunStatus.READY
        except ChatRuntimeError as exc:
            if worker.is_cancelled or generation != self._generation:
                return
            self._host.write_request_message("Error", exc.user_message)
            self._write_applied_change_warning(applied_changes.paths())
            self._write_elapsed_time(started_at)
            self._host.run_status = RunStatus.ERROR
        except Exception:
            if worker.is_cancelled or generation != self._generation:
                return
            self._host.write_request_message("Error", "Unexpected internal error")
            self._write_applied_change_warning(applied_changes.paths())
            self._write_elapsed_time(started_at)
            self._host.run_status = RunStatus.ERROR
        finally:
            if generation == self._generation:
                self._host.refresh_request_skill_status()
                self.finish_stream(generation)
                self._stop_activity(generation)
                self._active_worker = None
                self._host.focus_request_prompt()

    async def _approve_tool(
        self,
        request: AnyToolApprovalRequest,
        generation: int,
    ) -> bool:
        if generation != self._generation:
            return False
        self._host.run_status = RunStatus.AWAITING_APPROVAL
        approved = await self._host.push_screen_wait(
            ToolApprovalScreen(request)
        )
        if generation != self._generation:
            return False
        self._host.run_status = RunStatus.THINKING
        return approved is True

    def _start_activity(self, started_at: float, generation: int) -> None:
        self._stop_activity()
        self._activity_started_at = started_at
        self._activity_generation = generation
        self._host.query_one(ActivityBar).show_activity(
            0.0,
            self._host.run_status,
        )
        self._activity_timer = self._host.set_interval(
            self._activity_interval_seconds,
            lambda: self.refresh_activity(generation),
            name="request-activity",
        )

    def _stop_activity(self, expected_generation: int | None = None) -> None:
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
        if self._host.is_mounted:
            self._host.query_one(ActivityBar).reset_activity()

    def _begin_stream(self, generation: int) -> None:
        self.finish_stream()
        self._stream_generation = generation

    def _append_stream_delta(self, delta: str, generation: int) -> None:
        if (
            not isinstance(delta, str)
            or not delta
            or generation != self._stream_generation
            or generation != self._generation
        ):
            return
        self._host.query_one(StreamOutput).append_delta(delta)

    def _flush_stream(self, generation: int) -> None:
        if generation == self._stream_generation:
            self._host.query_one(StreamOutput).flush()

    def _reset_stream(self, generation: int) -> None:
        if generation != self._stream_generation:
            return
        if self._host.is_mounted:
            self._host.query_one(StreamOutput).reset_output()

    def finish_stream(self, expected_generation: int | None = None) -> None:
        if (
            expected_generation is not None
            and expected_generation != self._stream_generation
        ):
            return
        self._stream_generation = None
        if self._host.is_mounted:
            self._host.query_one(StreamOutput).reset_output()

    def _write_applied_change_warning(self, paths: tuple[str, ...]) -> None:
        if paths:
            self._host.write_request_message(
                "System",
                "本轮已写入但尚未完成：" + "、".join(paths),
            )

    def _write_elapsed_time(self, started_at: float) -> None:
        elapsed = self._clock() - started_at
        self._host.write_request_message(
            "System",
            f"耗时：{elapsed:.2f} 秒",
        )

    def _write_request_statistics(
        self,
        started_at: float,
        usage: TokenUsage | None,
    ) -> None:
        elapsed = self._clock() - started_at
        if usage is None:
            token_text = "Token：不可用"
        else:
            token_text = (
                f"Token：输入 {usage.input_tokens} | "
                f"输出 {usage.output_tokens} | 合计 {usage.total_tokens}"
            )
        self._host.write_request_message(
            "System",
            f"耗时：{elapsed:.2f} 秒 | {token_text}",
        )
