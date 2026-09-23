"""Web UI 对共享 ChatSession 的多会话适配与流式事件编排。"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Literal

from app.observability.model_logging import log_web_statistics_error
from app.runtime.chat import ChatErrorCode, ChatRuntimeError, ChatRuntimeInfo, get_chat_runtime_info
from app.runtime.context_settings import ContextSettings
from app.runtime.context_settings_store import ContextSettingsConflict, ContextSettingsError, ContextSettingsStore
from app.runtime.model_budget import ModelBudgetCatalog
from app.runtime.model_selection import ModelSelectionError, ModelSelectionService
from app.runtime.model_selection_store import ModelSelectionStore
from app.runtime.personalization_store import PersonalizationError, PersonalizationStore
from app.runtime.session import ChatExecutionSnapshot, ChatSession, RuntimeBudgetSnapshot
from app.runtime.session_store import SessionStore
from app.runtime.workspace_changes import AppliedChangeTracker
from app.runtime.system_prompt import SystemPromptLoadError, compose_system_prompt, load_system_prompt
from app.runtime.tool_loop import WORKSPACE_TOOL_LOOP_LIMITS
from app.services.llm.contracts import LlmProvider, ModelOption, TokenUsage
from app.services.llm.factory import resolve_model_options
from app.webui.approvals import WebApprovalCoordinator
from app.webui.directory_picker import directory_picker_available
from app.webui.projects import WebProjectCatalog, WebProjectRecord, normalize_project_path
from app.webui.sessions import WebSessionCatalog, WebSessionStoreError
from app.webui.statistics import (
    WebRequestStatistic,
    WebStatisticsStore,
    WebStatisticsStoreError,
)
from tools.contracts import AnyToolApprovalRequest, ToolCall, ToolResult
from tools.workspace import (
    ListWorkspaceFilesTool,
    ReadWorkspaceFileTool,
    WorkspacePolicy,
    create_web_intent_workspace_registry,
)
from tools.mcp_client import McpRegistry, load_mcp_config
from local_paths import data_root


DATA_ROOT = data_root()
DEFAULT_WEB_SESSION_PATH = DATA_ROOT / "web-session.json"
DEFAULT_WEB_SESSIONS_ROOT = DATA_ROOT / "web-sessions"
DEFAULT_WEB_STATISTICS_PATH = DATA_ROOT / "web-statistics.json"
DEFAULT_WEB_PROJECTS_PATH = DATA_ROOT / "web-projects.json"
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
        statistics: WebStatisticsStore,
        workspace: Path,
        workspace_policy: WorkspacePolicy,
        *,
        context_window_tokens: int,
        startup_warning: str | None = None,
        system_prompt_loaded: bool = False,
        context_settings: ContextSettings | None = None,
        personalization_store: PersonalizationStore | None = None,
        projects: WebProjectCatalog | None = None,
    ) -> None:
        self.catalog = catalog
        self._session_factory = session_factory
        self._sessions: dict[str, ChatSession] = {}
        self._provider_override: LlmProvider | None = None
        self.runtime_info = runtime_info
        self.model_options = tuple(model_options)
        self.model_selection = model_selection
        self.statistics = statistics
        self.workspace = workspace
        self.workspace_policy = workspace_policy
        self.projects = projects or WebProjectCatalog(workspace / "data" / "web-projects.json", workspace)
        self.context_window_tokens = context_window_tokens
        self.startup_warning = startup_warning
        self.system_prompt_loaded = system_prompt_loaded
        self.context_settings = context_settings or ContextSettings(
            ModelBudgetCatalog({}), ContextSettingsStore(workspace / "data" / "context-settings.json"),
        )
        self.personalization_store = personalization_store or PersonalizationStore(
            workspace / "data" / "personalization.json",
        )
        self._request_lock = asyncio.Lock()
        self._active_task: asyncio.Task[None] | None = None
        self._active_request_id: str | None = None
        self._approvals = WebApprovalCoordinator()
        self._activate_project(self.projects.require(self.catalog.current.project_id), validate=False)

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
        projects = WebProjectCatalog(DEFAULT_WEB_PROJECTS_PATH, root)
        catalog = WebSessionCatalog(DEFAULT_WEB_SESSIONS_ROOT, legacy_path=DEFAULT_WEB_SESSION_PATH)
        for project_id in catalog.project_ids():
            projects.require(project_id)
        context_settings = ContextSettings(ModelBudgetCatalog(values), ContextSettingsStore())
        startup_warning = None

        personalization_store = PersonalizationStore()
        try:
            personalization_store.load()
        except PersonalizationError:
            startup_warning = startup_warning or "个性化设置无法加载，Web 会话暂未使用自定义提示词。"

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
            project = projects.require(catalog.current.project_id)
            try:
                active_policy = _project_workspace_policy(project)
            except (OSError, ValueError) as exc:
                raise ChatRuntimeError(ChatErrorCode.INVALID_INPUT, "项目路径不可用，请修改项目配置。") from exc
            try:
                system_prompt = load_system_prompt(active_policy.root)
            except SystemPromptLoadError:
                system_prompt = None
            registry = create_web_intent_workspace_registry(
                active_policy,
                web_search_environ=values,
            )
            try:
                configs = load_mcp_config()
            except ValueError as exc:
                raise ChatRuntimeError(ChatErrorCode.CONFIGURATION, "MCP 配置无效，请检查 data/mcp-servers.json。") from exc
            if configs:
                registry = McpRegistry(
                    lambda mcp_tools: create_web_intent_workspace_registry(
                        active_policy,
                        web_search_environ=values,
                        mcp_tools=mcp_tools,
                    ),
                    configs,
                )
            return ChatExecutionSnapshot(
                system_prompt=compose_system_prompt(system_prompt, personalization_store.current_prompt),
                registry=registry,
            )

        def session_factory(store: SessionStore) -> ChatSession:
            current_provider = selection.restore().provider or provider
            return ChatSession.load(
                store,
                provider=current_provider,
                execution_snapshot_provider=execution_snapshot,
                tool_loop_limits=WORKSPACE_TOOL_LOOP_LIMITS,
                budget_snapshot_provider=lambda active: _runtime_budget_snapshot(
                    context_settings, active,
                ),
            )

        return cls(
            catalog,
            session_factory,
            runtime_info,
            options,
            selection,
            WebStatisticsStore(DEFAULT_WEB_STATISTICS_PATH),
            root,
            policy,
            context_window_tokens=(
                context_settings.snapshot(runtime_info.provider, runtime_info.model).budget.context_window_tokens
                if runtime_info.provider in {"deepseek", "aliyun"} else 128_000
            ),
            startup_warning=warning,
            system_prompt_loaded=False,
            context_settings=context_settings,
            personalization_store=personalization_store,
            projects=projects,
        )

    def personalization_payload(self) -> dict[str, object]:
        """显式读取最新个性化设置，供页面发现其他标签页的变更。"""

        return self.personalization_store.load()

    def save_personalization(self, *, expected_revision: int, prompt: str) -> dict[str, object]:
        """保存后仅影响下一次模型请求的执行快照。"""

        return self.personalization_store.save(expected_revision=expected_revision, prompt=prompt)

    def context_settings_payload(self) -> dict:
        return self.context_settings.payload(self.runtime_info.provider, self.runtime_info.model)

    def save_context_settings(self, payload: dict) -> dict:
        self._require_idle("保存上下文设置")
        provider, model = self.runtime_info.provider, self.runtime_info.model
        if payload["scope"] == "model" and (payload.get("provider"), payload.get("model")) != (provider, model):
            raise ContextSettingsConflict("当前模型已变化，请重新加载设置。")
        return self.context_settings.save(
            expected_revision=payload["expected_revision"], scope=payload["scope"],
            overrides=payload["overrides"], provider=provider, model=model,
            configured_models=tuple((item.provider, item.model) for item in self.model_options),
        )

    def bootstrap(self) -> dict[str, object]:
        """返回首屏需要的脱敏状态，不暴露系统提示或密钥。"""

        return {
            "project_name": "Tsi 助手",
            **self._conversation_payload(),
            "runtime": self._runtime_payload(),
            "models": [self._model_payload(option) for option in self.model_options],
            "startup_warning": self.startup_warning,
            "capabilities": {
                "streaming": True,
                "web_search": True,
                "workspace_read": True,
                "workspace_write": True,
                "native_directory_picker": directory_picker_available(),
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
            request_provider = self.runtime_info.provider
            request_model = self.runtime_info.model
            statistics_recorded = False
            applied_changes = AppliedChangeTracker()

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
                applied_changes.observe(call, result)
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

                def record_statistics(
                    outcome: Literal["completed", "failed", "cancelled"],
                    token_usage: TokenUsage | None = None,
                ) -> None:
                    nonlocal statistics_recorded
                    if statistics_recorded:
                        return
                    statistics_recorded = True
                    elapsed_ms = (time.monotonic() - started_at) * 1000
                    try:
                        self.statistics.record(
                            WebRequestStatistic(
                                outcome,
                                request_provider,
                                request_model,
                                elapsed_ms,
                                token_usage,
                            )
                        )
                    except (WebStatisticsStoreError, ValueError) as exc:
                        # 统计属于诊断旁路，失败不能覆盖真实模型终态。
                        log_web_statistics_error(
                            request_id=request_id,
                            operation="record",
                            error_type=type(exc).__name__,
                        )

                try:
                    def on_context_event(event: dict[str, object]) -> None:
                        event_type = str(event["type"])
                        emit(event_type, **{key: value for key, value in event.items() if key not in {"type", "request_id"}})

                    result = await active_session.send(
                        input_text,
                        on_text_delta=lambda text: emit("text_delta", text=text),
                        on_text_reset=lambda: emit("text_reset"),
                        on_tool_approval=on_tool_approval,
                        on_tool_result=on_tool_result,
                        on_context_event=on_context_event,
                    )
                except asyncio.CancelledError:
                    record_statistics("cancelled")
                    emit("cancelled", applied_changes=applied_changes.paths())
                    return
                except ChatRuntimeError as exc:
                    record_statistics("failed")
                    emit("failed", code=exc.code.value, message=exc.user_message,
                         applied_changes=applied_changes.paths())
                    return
                except Exception:
                    # 未知内部错误只返回稳定文案，具体诊断留在服务端日志。
                    record_statistics("failed")
                    emit("failed", code="internal", message="Web UI 请求失败。",
                         applied_changes=applied_changes.paths())
                    return
                usage = result.token_usage
                metadata_warning = None
                try:
                    record = self.catalog.touch(session_id, first_input=input_text)
                except WebSessionStoreError:
                    # 对话内容已经由 ChatSession 持久化，索引异常不能让流悬挂。
                    record = self.catalog.current
                    metadata_warning = "消息已保存，但会话列表更新失败。"
                record_statistics("completed", usage)
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
                    finish_reason=result.finish_reason,
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

    def statistics_payload(self) -> dict[str, object]:
        """返回不含请求正文、会话标识和密钥的聚合统计。"""

        return self.statistics.snapshot()

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
        self.catalog.create(self.catalog.current.project_id)
        return self._conversation_payload()

    def select_session(self, session_id: str) -> dict[str, object]:
        """切换到已存在的会话并恢复其完整页面状态。"""

        self._require_idle("切换")
        project = self.projects.require(self.catalog.record(session_id).project_id)
        self._validate_project(project)
        self.catalog.select(session_id)
        self._activate_project(project)
        return self._conversation_payload()

    def create_project(self, *, name: str, path: str) -> dict[str, object]:
        """新增项目并为其创建一个空会话，保留原项目所有历史。"""

        self._require_idle("新增")
        self.catalog.ensure_capacity()
        project = self.projects.create(name, path)
        self.catalog.create(project.id)
        self._activate_project(project)
        return self._conversation_payload()

    def select_project(self, project_id: str) -> dict[str, object]:
        """选择项目内最近会话；空项目首次选择时创建会话。"""

        self._require_idle("切换")
        project = self.projects.require(project_id)
        self._validate_project(project)
        recent = self.catalog.latest_in_project(project_id)
        if recent is None:
            self.catalog.create(project_id)
        else:
            self.catalog.select(recent.id)
        self._activate_project(project)
        return self._conversation_payload()

    def update_project(self, project_id: str, *, name: str, path: str) -> dict[str, object]:
        """路径更改只作用于下一轮请求，不改写历史消息。"""

        self._require_idle("修改")
        project = self.projects.update(project_id, name=name, path=path)
        if project.id == self.catalog.current.project_id:
            self._activate_project(project)
        return self._conversation_payload()

    def _validate_project(self, project: WebProjectRecord) -> WorkspacePolicy:
        try:
            return _project_workspace_policy(project)
        except (OSError, ValueError) as exc:
            raise ValueError("项目路径不可用，请修改项目配置。") from exc

    def _activate_project(self, project: WebProjectRecord, *, validate: bool = True) -> None:
        """切换文件浏览和规则状态；失效项目仅在启动恢复时容忍。"""

        try:
            policy = self._validate_project(project)
        except ValueError:
            if validate:
                raise
            self.workspace = Path(project.path)
            self.workspace_policy = None
            self.system_prompt_loaded = False
            self._project_warning = "项目路径不可用，请修改项目配置。"
            return
        self.workspace = policy.root
        self.workspace_policy = policy
        try:
            self.system_prompt_loaded = load_system_prompt(policy.root) is not None
            self._project_warning = None
        except SystemPromptLoadError:
            self.system_prompt_loaded = False
            self._project_warning = "AGENTS.md 无法加载，本项目暂未使用项目规则。"

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
        self._activate_project(self.projects.require(self.catalog.current.project_id), validate=False)
        return {**self._conversation_payload(), "warning": self.catalog.last_cleanup_warning}

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
        try:
            self.context_settings.snapshot(provider, model)
        except ContextSettingsError as exc:
            raise ModelSelectionError("目标模型的上下文预算不可用。") from exc
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

        policy = self._validate_project(self.projects.require(self.catalog.current.project_id))
        return await ListWorkspaceFilesTool(policy).invoke(
            {"path": ".", "depth": 4, "cursor": 0, "limit": 200}
        )

    async def preview_file(self, path: str) -> dict[str, object]:
        """使用现有只读工具预览有界 UTF-8 文本。"""

        policy = self._validate_project(self.projects.require(self.catalog.current.project_id))
        return await ReadWorkspaceFileTool(policy).invoke(
            {"path": path, "start_line": 1, "max_lines": 400}
        )

    def _conversation_payload(self) -> dict[str, object]:
        active_session = self.session
        return {
            "projects": [project.to_payload() for project in self.projects.list_records()],
            "current_project_id": self.catalog.current.project_id,
            "workspace_name": self.projects.require(self.catalog.current.project_id).name,
            "workspace_path": str(self.workspace),
            "system_prompt_loaded": self.system_prompt_loaded,
            "workspace_warning": self._project_warning,
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
        if self.runtime_info.provider not in {"deepseek", "aliyun"}:
            return 0.0
        try:
            return active_session.preview_context_snapshot()["percent"]
        except ChatRuntimeError:
            return 0.0


def _runtime_budget_snapshot(settings: ContextSettings, provider: LlmProvider) -> RuntimeBudgetSnapshot:
    snapshot = settings.snapshot(provider.name, provider.model)
    return RuntimeBudgetSnapshot(
        snapshot.budget, snapshot.revision,
        ",".join(sorted(set(snapshot.sources.values()))),
        snapshot.warning,
    )


def _project_workspace_policy(project: WebProjectRecord) -> WorkspacePolicy:
    """每次请求重新验证持久化路径，防止目录后来被替换或改为宽范围。"""

    normalized = normalize_project_path(project.path)
    if normalized != project.path:
        raise ValueError("project path changed")
    return WorkspacePolicy(Path(normalized))
