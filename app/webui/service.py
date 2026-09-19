"""Web UI 对共享 ChatSession 的多会话适配与流式事件编排。"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path

from app.runtime.chat import ChatRuntimeError, ChatRuntimeInfo, get_chat_runtime_info
from app.runtime.memory import estimate_messages_tokens, resolve_memory_policy
from app.runtime.model_selection import ModelSelectionError, ModelSelectionService
from app.runtime.model_selection_store import ModelSelectionStore
from app.runtime.session import ChatExecutionSnapshot, ChatSession
from app.runtime.session_store import SessionStore
from app.runtime.system_prompt import SystemPromptLoadError, load_system_prompt
from app.runtime.tool_loop import WORKSPACE_TOOL_LOOP_LIMITS
from app.services.llm.contracts import LlmProvider, ModelOption
from app.services.llm.factory import resolve_model_options
from app.webui.approvals import WebApprovalCoordinator
from app.webui.sessions import WebSessionCatalog, WebSessionStoreError
from tools.contracts import AnyToolApprovalRequest, ToolCall, ToolResult
from tools.workspace import (
    ListWorkspaceFilesTool,
    ReadWorkspaceFileTool,
    WorkspacePolicy,
    create_web_intent_workspace_registry,
)


DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
DEFAULT_WEB_SESSION_PATH = DATA_ROOT / "web-session.json"
DEFAULT_WEB_SESSIONS_ROOT = DATA_ROOT / "web-sessions"
SessionFactory = Callable[[SessionStore], ChatSession]


class WebUiBusyError(Exception):
    """Web UI 已有模型请求运行时拒绝并发或会话变更。"""


class WebUiService:
    """维护 Web 多会话，并把 Runtime 回调转换为安全事件。"""

    def __init__(
        self,
        catalog: WebSessionCatalog,
        session_factory: SessionFactory,
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
        self.catalog = catalog
        self._session_factory = session_factory
        self._sessions: dict[str, ChatSession] = {}
        self._provider_override: LlmProvider | None = None
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
        self._active_request_id: str | None = None
        self._approvals = WebApprovalCoordinator()

    @property
    def session(self) -> ChatSession:
        """返回当前会话对象，并按索引延迟恢复持久化状态。"""

        session_id = self.catalog.current.id
        session = self._sessions.get(session_id)
        if session is None:
            session = self._session_factory(self.catalog.session_store(session_id))
            if self._provider_override is not None:
                session.replace_provider(self._provider_override)
            self._sessions[session_id] = session
        return session

    @classmethod
    def production(
        cls,
        workspace: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "WebUiService":
        """从启动目录和环境配置装配带逐次写审批的 Web 多会话。"""

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
                registry=create_web_intent_workspace_registry(policy),
            )

        def session_factory(store: SessionStore) -> ChatSession:
            current_provider = selection.restore().provider or provider
            return ChatSession.load(
                store,
                provider=current_provider,
                execution_snapshot_provider=execution_snapshot,
                tool_loop_limits=WORKSPACE_TOOL_LOOP_LIMITS,
                memory_policy=memory_policy,
            )

        return cls(
            WebSessionCatalog(
                DEFAULT_WEB_SESSIONS_ROOT,
                legacy_path=DEFAULT_WEB_SESSION_PATH,
            ),
            session_factory,
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
            **self._conversation_payload(),
            "runtime": self._runtime_payload(),
            "models": [self._model_payload(option) for option in self.model_options],
            "startup_warning": self.startup_warning,
            "system_prompt_loaded": self.system_prompt_loaded,
            "capabilities": {
                "streaming": True,
                "workspace_read": True,
                "workspace_write": True,
                "skills": False,
            },
        }

    async def stream_message(self, input_text: str) -> AsyncIterator[dict[str, object]]:
        """在当前会话执行一次请求并产生有序、有限的浏览器事件。"""

        if self._request_lock.locked():
            raise WebUiBusyError("已有请求正在运行。")
        async with self._request_lock:
            session_id = self.catalog.current.id
            active_session = self.session
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
                        "session_id": session_id,
                        **payload,
                    }
                )

            def on_tool_result(call: ToolCall, result: ToolResult) -> None:
                emit(
                    "tool_finished",
                    tool=call.name,
                    status="error" if result.is_error else "success",
                )

            async def on_tool_approval(
                approval: AnyToolApprovalRequest,
            ) -> bool:
                return await self._approvals.request(
                    request_id,
                    approval,
                    lambda payload: emit("tool_approval_required", **payload),
                )

            async def run() -> None:
                emit("request_started")
                try:
                    result = await active_session.send(
                        input_text,
                        on_text_delta=lambda text: emit("text_delta", text=text),
                        on_text_reset=lambda: emit("text_reset"),
                        on_tool_approval=on_tool_approval,
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
                metadata_warning = None
                try:
                    record = self.catalog.touch(session_id, first_input=input_text)
                except WebSessionStoreError:
                    # 对话内容已经由 ChatSession 持久化，索引异常不能让流悬挂。
                    record = self.catalog.current
                    metadata_warning = "消息已保存，但会话列表更新失败。"
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
                    context_percent=self._context_percent(active_session),
                    session=record.to_payload(),
                    sessions=self._sessions_payload(),
                    warning=metadata_warning,
                )

            runner = asyncio.create_task(run())
            self._active_task = runner
            self._active_request_id = request_id
            try:
                while True:
                    event = await queue.get()
                    yield event
                    if event["type"] in {"completed", "failed", "cancelled"}:
                        break
                await runner
            finally:
                self._approvals.invalidate(request_id)
                if not runner.done():
                    runner.cancel()
                    await asyncio.gather(runner, return_exceptions=True)
                if self._active_task is runner:
                    self._active_task = None
                if self._active_request_id == request_id:
                    self._active_request_id = None

    def cancel_current(self) -> bool:
        """取消当前模型请求；没有活动请求时返回 False。"""

        task = self._active_task
        if task is None or task.done():
            return False
        if self._active_request_id is not None:
            self._approvals.invalidate(self._active_request_id)
        task.cancel()
        return True

    def resolve_tool_approval(
        self,
        approval_id: str,
        request_id: str,
        approved: bool,
    ) -> dict[str, bool]:
        """把页面的一次性决定提交给当前等待中的工具调用。"""

        return self._approvals.resolve(approval_id, request_id, approved)

    @property
    def is_busy(self) -> bool:
        """返回全局 Web 模型请求是否正在运行。"""

        return self._request_lock.locked()

    def clear(self) -> dict[str, object]:
        """清空当前 Web 会话，保留标题、长期偏好和模型选择。"""

        self._require_idle("清空")
        self.session.clear()
        self.catalog.touch(self.catalog.current.id)
        return self._conversation_payload()

    def create_session(self) -> dict[str, object]:
        """创建并选中一个独立空会话。"""

        self._require_idle("创建")
        self.catalog.create()
        return self._conversation_payload()

    def select_session(self, session_id: str) -> dict[str, object]:
        """切换到已存在的会话并恢复其完整页面状态。"""

        self._require_idle("切换")
        self.catalog.select(session_id)
        return self._conversation_payload()

    def rename_session(self, session_id: str, title: str) -> dict[str, object]:
        """重命名指定会话并返回当前页面状态。"""

        self._require_idle("重命名")
        self.catalog.rename(session_id, title)
        return self._conversation_payload()

    def delete_session(self, session_id: str) -> dict[str, object]:
        """删除指定会话，并保证删除后仍存在有效当前会话。"""

        self._require_idle("删除")
        self.catalog.delete(session_id)
        self._sessions.pop(session_id, None)
        return self._conversation_payload()

    def select_model(self, provider: str, model: str) -> dict[str, object]:
        """切换项目级模型，并同步所有已缓存的 Web 会话。"""

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
        current_id = self.catalog.current.id
        current_session = self.session
        result = self.model_selection.switch(current_session, option)
        self.runtime_info = result.runtime_info
        self._provider_override = result.provider
        for session_id, session in self._sessions.items():
            if session_id != current_id:
                session.replace_provider(result.provider)
        payload = self._runtime_payload()
        payload["warning"] = result.warning
        return payload

    async def list_files(self) -> dict[str, object]:
        """使用现有 Workspace Policy 返回浅层安全文件树。"""

        return await ListWorkspaceFilesTool(self.workspace_policy).invoke(
            {"path": ".", "depth": 4, "cursor": 0, "limit": 200}
        )

    async def preview_file(self, path: str) -> dict[str, object]:
        """使用现有只读工具预览有界 UTF-8 文本。"""

        return await ReadWorkspaceFileTool(self.workspace_policy).invoke(
            {"path": path, "start_line": 1, "max_lines": 400}
        )

    def _conversation_payload(self) -> dict[str, object]:
        active_session = self.session
        return {
            "sessions": self._sessions_payload(),
            "current_session_id": self.catalog.current.id,
            "current_session": self.catalog.current.to_payload(),
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in active_session.messages
            ],
            "context_percent": self._context_percent(active_session),
        }

    def _sessions_payload(self) -> list[dict[str, object]]:
        return [record.to_payload() for record in self.catalog.list_records()]

    def _require_idle(self, action: str) -> None:
        if self._request_lock.locked():
            raise WebUiBusyError(f"请求运行期间不能{action}会话。")

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

    def _context_percent(self, session: ChatSession | None = None) -> float:
        active_session = session or self.session
        used = estimate_messages_tokens(active_session.messages)
        return round(min(100.0, used * 100 / self.context_window_tokens), 1)
