"""Web UI 对共享 ChatSession 的薄适配与流式事件编排。"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from app.runtime.chat import ChatRuntimeError, ChatRuntimeInfo, get_chat_runtime_info
from app.runtime.memory import estimate_messages_tokens, resolve_memory_policy
from app.runtime.model_selection import (
    ModelSelectionError,
    ModelSelectionService,
)
from app.runtime.model_selection_store import ModelSelectionStore
from app.runtime.session import ChatExecutionSnapshot, ChatSession
from app.runtime.session_store import SessionStore
from app.runtime.system_prompt import SystemPromptLoadError, load_system_prompt
from app.runtime.tool_loop import WORKSPACE_TOOL_LOOP_LIMITS
from app.services.llm.contracts import ChatRole, ModelOption
from app.services.llm.factory import resolve_model_options
from tools.contracts import ToolCall, ToolResult
from tools.workspace import (
    ListWorkspaceFilesTool,
    ReadWorkspaceFileTool,
    WorkspacePolicy,
    create_readonly_intent_workspace_registry,
)


DEFAULT_WEB_SESSION_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "web-session.json"
)


class WebUiBusyError(Exception):
    """Web UI 已有模型请求运行时拒绝并发发送或切换模型。"""


class WebUiService:
    """维护 Web UI 唯一会话，并把 Runtime 回调转换为安全事件。"""

    def __init__(
        self,
        session: ChatSession,
        runtime_info: ChatRuntimeInfo,
        model_options: tuple[ModelOption, ...],
        model_selection: ModelSelectionService,
        workspace: Path,
        workspace_policy: WorkspacePolicy,
        *,
        context_window_tokens: int,
        startup_warning: str | None = None,
        system_prompt_loaded: bool = False,
    ) -> None:
        self.session = session
        self.runtime_info = runtime_info
        self.model_options = tuple(model_options)
        self.model_selection = model_selection
        self.workspace = workspace
        self.workspace_policy = workspace_policy
        self.context_window_tokens = context_window_tokens
        self.startup_warning = startup_warning
        self.system_prompt_loaded = system_prompt_loaded
        self._request_lock = asyncio.Lock()
        self._active_task: asyncio.Task[None] | None = None

    @classmethod
    def production(
        cls,
        workspace: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "WebUiService":
        """从启动目录和环境配置装配不含写能力的 Web 会话。"""

        root = (workspace or Path.cwd()).resolve()
        values = os.environ if environ is None else environ
        policy = WorkspacePolicy(root)
        memory_policy = resolve_memory_policy(values)
        try:
            system_prompt = load_system_prompt(root)
            startup_warning = None
        except SystemPromptLoadError:
            system_prompt = None
            startup_warning = "AGENTS.md 无法加载，Web 会话未使用项目规则。"

        options = resolve_model_options(values)
        selection = ModelSelectionService(options, ModelSelectionStore())
        restored = selection.restore()
        provider = restored.provider
        warning = restored.warning or startup_warning
        if provider is not None:
            runtime_info = ChatRuntimeInfo(
                provider.name,
                provider.model,
                provider.api_key_configured,
            )
        else:
            try:
                runtime_info = get_chat_runtime_info()
            except ChatRuntimeError as exc:
                runtime_info = ChatRuntimeInfo("unknown", "-", False)
                warning = warning or exc.user_message

        def execution_snapshot(_input_text: str) -> ChatExecutionSnapshot:
            return ChatExecutionSnapshot(
                system_prompt=system_prompt,
                registry=create_readonly_intent_workspace_registry(policy),
            )

        session = ChatSession.load(
            SessionStore(DEFAULT_WEB_SESSION_PATH),
            provider=provider,
            execution_snapshot_provider=execution_snapshot,
            tool_loop_limits=WORKSPACE_TOOL_LOOP_LIMITS,
            memory_policy=memory_policy,
        )
        return cls(
            session,
            runtime_info,
            options,
            selection,
            root,
            policy,
            context_window_tokens=memory_policy.context_window_tokens,
            startup_warning=warning,
            system_prompt_loaded=system_prompt is not None,
        )

    def bootstrap(self) -> dict[str, object]:
        """返回首屏需要的脱敏状态，不暴露系统提示或密钥。"""

        return {
            "project_name": "Tsi 助手",
            "workspace_name": self.workspace.name or str(self.workspace),
            "workspace_path": str(self.workspace),
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in self.session.messages
            ],
            "runtime": self._runtime_payload(),
            "models": [self._model_payload(option) for option in self.model_options],
            "context_percent": self._context_percent(),
            "startup_warning": self.startup_warning,
            "system_prompt_loaded": self.system_prompt_loaded,
            "capabilities": {
                "streaming": True,
                "workspace_read": True,
                "workspace_write": False,
                "skills": False,
            },
        }

    async def stream_message(self, input_text: str) -> AsyncIterator[dict[str, object]]:
        """执行一次请求并产生有序、有限的浏览器事件。"""

        if self._request_lock.locked():
            raise WebUiBusyError("已有请求正在运行。")
        async with self._request_lock:
            request_id = secrets.token_hex(12)
            sequence = 0
            queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            started_at = time.monotonic()

            def emit(event_type: str, **payload: object) -> None:
                nonlocal sequence
                sequence += 1
                queue.put_nowait(
                    {
                        "type": event_type,
                        "request_id": request_id,
                        "sequence": sequence,
                        **payload,
                    }
                )

            def on_tool_result(call: ToolCall, result: ToolResult) -> None:
                emit(
                    "tool_finished",
                    tool=call.name,
                    status="error" if result.is_error else "success",
                )

            async def run() -> None:
                emit("request_started")
                try:
                    result = await self.session.send(
                        input_text,
                        on_text_delta=lambda text: emit("text_delta", text=text),
                        on_text_reset=lambda: emit("text_reset"),
                        on_tool_result=on_tool_result,
                    )
                except asyncio.CancelledError:
                    emit("cancelled")
                    return
                except ChatRuntimeError as exc:
                    emit("failed", code=exc.code.value, message=exc.user_message)
                    return
                except Exception:
                    # 未知内部错误只返回稳定文案，具体诊断留在服务端日志。
                    emit("failed", code="internal", message="Web UI 请求失败。")
                    return
                usage = result.token_usage
                emit(
                    "completed",
                    output_text=result.output_text,
                    elapsed_ms=round((time.monotonic() - started_at) * 1000, 2),
                    token_usage=(
                        {
                            "input": usage.input_tokens,
                            "output": usage.output_tokens,
                            "total": usage.total_tokens,
                        }
                        if usage is not None
                        else None
                    ),
                    context_percent=self._context_percent(),
                )

            runner = asyncio.create_task(run())
            self._active_task = runner
            try:
                while True:
                    event = await queue.get()
                    yield event
                    if event["type"] in {"completed", "failed", "cancelled"}:
                        break
                await runner
            finally:
                if not runner.done():
                    runner.cancel()
                    await asyncio.gather(runner, return_exceptions=True)
                if self._active_task is runner:
                    self._active_task = None

    def cancel_current(self) -> bool:
        """取消当前模型请求；没有活动请求时返回 False。"""

        task = self._active_task
        if task is None or task.done():
            return False
        task.cancel()
        return True

    @property
    def is_busy(self) -> bool:
        """供路由在创建流响应前执行无副作用的并发检查。"""

        return self._request_lock.locked()

    def clear(self) -> None:
        """清空 Web 独立会话，保留长期偏好和模型选择。"""

        if self._request_lock.locked():
            raise WebUiBusyError("请求运行期间不能清空会话。")
        self.session.clear()

    def select_model(self, provider: str, model: str) -> dict[str, object]:
        """复用 Runtime 模型选择用例，并更新后续 Web 请求。"""

        if self._request_lock.locked():
            raise WebUiBusyError("请求运行期间不能切换模型。")
        option = next(
            (
                item
                for item in self.model_options
                if item.provider == provider and item.model == model
            ),
            None,
        )
        if option is None:
            raise ModelSelectionError("模型切换失败。")
        result = self.model_selection.switch(self.session, option)
        self.runtime_info = result.runtime_info
        payload = self._runtime_payload()
        payload["warning"] = result.warning
        return payload

    async def list_files(self) -> dict[str, object]:
        """使用现有 Workspace Policy 返回浅层安全文件树。"""

        return await ListWorkspaceFilesTool(self.workspace_policy).invoke(
            {"path": ".", "depth": 4, "cursor": 0, "limit": 200}
        )

    async def preview_file(self, path: str) -> dict[str, object]:
        """使用现有只读工具预览 UTF-8 文本。"""

        return await ReadWorkspaceFileTool(self.workspace_policy).invoke(
            {"path": path, "start_line": 1, "max_lines": 400}
        )

    def _runtime_payload(self) -> dict[str, object]:
        return {
            "provider": self.runtime_info.provider,
            "model": self.runtime_info.model,
            "api_key_configured": self.runtime_info.api_key_configured,
        }

    @staticmethod
    def _model_payload(option: ModelOption) -> dict[str, object]:
        return {
            "provider": option.provider,
            "model": option.model,
            "api_key_configured": option.api_key_configured,
        }

    def _context_percent(self) -> float:
        used = estimate_messages_tokens(self.session.messages)
        return round(min(100.0, used * 100 / self.context_window_tokens), 1)
