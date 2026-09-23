"""在 Textual App 之外完成 TUI 运行依赖装配。"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.runtime.chat import (
    ChatRuntimeError,
    ChatRuntimeInfo,
    get_chat_runtime_info,
)
from app.runtime.memory import MemoryPolicy, resolve_memory_policy
from app.runtime.context_settings import ContextSettings
from app.runtime.context_settings_store import ContextSettingsError, ContextSettingsStore
from app.runtime.model_budget import ModelBudget, ModelBudgetCatalog
from app.runtime.model_selection import (
    ModelSelectionService,
    ProviderFactory,
)
from app.runtime.model_selection_store import ModelSelectionStore
from app.runtime.session import ChatSession, RuntimeBudgetSnapshot
from app.runtime.session_store import SessionStore
from app.runtime.task_runs import TaskRunStore, TaskRunError
from app.runtime.skill_runtime import SkillRuntime
from app.runtime.tool_loop import (
    DEFAULT_TOOL_LOOP_LIMITS,
    WORKSPACE_TOOL_LOOP_LIMITS,
)
from app.services.llm.contracts import LlmProvider, ModelOption
from app.services.llm.factory import (
    create_provider_for_model,
    resolve_model_options,
)
from app.tui.state import (
    IssueSeverity,
    StartupIssue,
    TuiHealthState,
)
from tools import ToolRuntime
from tools.workspace import WorkspacePolicy

if TYPE_CHECKING:
    from app.tui.request import ChatRunner, Clock


@dataclass(frozen=True)
class TuiDependencies:
    """构造 ChatTuiApp 所需的完整、有限进程内依赖。"""

    chat_session: ChatSession | None
    chat_runner: ChatRunner
    runtime_info: ChatRuntimeInfo
    model_selection: ModelSelectionService | None
    skill_runtime: SkillRuntime | None
    health: TuiHealthState
    system_prompt_loaded: bool
    workspace_enabled: bool
    skills_count: int
    clock: Clock
    task_policy: WorkspacePolicy | None = None
    task_store: TaskRunStore | None = None


def build_tui_dependencies(
    *,
    system_prompt: str | None,
    system_prompt_error: str | None,
    workspace_registry: ToolRuntime | None,
    workspace_error: str | None,
    skills_count: int,
    skills_error: str | None,
    skill_runtime: SkillRuntime | None,
    model_selection_store: ModelSelectionStore | None = None,
    model_options: tuple[ModelOption, ...] | None = None,
    session_store: SessionStore | None = None,
    runtime_info: ChatRuntimeInfo | None = None,
    initial_provider: LlmProvider | None = None,
    model_config_error: str | None = None,
    runtime_info_factory=get_chat_runtime_info,
    provider_factory: ProviderFactory = create_provider_for_model,
    memory_policy: MemoryPolicy | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Clock = time.monotonic,
    task_policy: WorkspacePolicy | None = None,
) -> TuiDependencies:
    """构造生产 TUI 依赖，同时把可恢复启动失败转换为诊断。"""

    values = os.environ if environ is None else environ
    issues: list[StartupIssue] = []
    if model_config_error is not None:
        issues.append(_issue("configuration", model_config_error, blocks_prompt=True))
    options = (
        resolve_model_options(values)
        if model_options is None
        else tuple(model_options)
    )
    model_selection = ModelSelectionService(
        options,
        model_selection_store,
        provider_factory=provider_factory,
    )
    restored = model_selection.restore()

    configuration_issue = None
    if restored.provider is not None:
        current_info = ChatRuntimeInfo(
            provider=restored.provider.name,
            model=restored.provider.model,
            api_key_configured=restored.provider.api_key_configured,
        )
    elif runtime_info is not None:
        current_info = runtime_info
    elif initial_provider is not None:
        current_info = ChatRuntimeInfo(
            initial_provider.name, initial_provider.model, initial_provider.api_key_configured,
        )
    else:
        try:
            current_info = runtime_info_factory()
        except ChatRuntimeError as exc:
            configuration_issue = _issue(
                "configuration",
                exc.user_message,
                blocks_prompt=True,
            )
            current_info = ChatRuntimeInfo("unknown", "-", False)
    if configuration_issue is not None:
        issues.append(configuration_issue)
    if restored.warning is not None:
        issues.append(
            StartupIssue(
                "model_selection",
                restored.warning,
                IssueSeverity.WARNING,
                blocks_prompt=False,
            )
        )

    if memory_policy is None:
        try:
            memory_policy = resolve_memory_policy(values)
        except ValueError as exc:
            if configuration_issue is None:
                issues.append(
                    _issue(
                        "configuration",
                        str(exc),
                        blocks_prompt=True,
                    )
                )
            memory_policy = MemoryPolicy()

    try:
        context_settings = ContextSettings(ModelBudgetCatalog(values), ContextSettingsStore())
        context_settings.snapshot(current_info.provider, current_info.model) if current_info.provider in {"deepseek", "aliyun"} else None
    except (ValueError, ContextSettingsError) as exc:
        issues.append(_issue("configuration", str(exc), blocks_prompt=True))
        context_settings = None

    def budget_snapshot(active_provider):
        if context_settings is not None and active_provider.name in {"deepseek", "aliyun"}:
            resolved = context_settings.snapshot(active_provider.name, active_provider.model)
            return RuntimeBudgetSnapshot(
                resolved.budget, resolved.revision,
                ",".join(sorted(set(resolved.sources.values()))),
                resolved.warning,
            )
        # 显式测试策略仍可构造对应窗口；生产路径由 ContextSettings 提供。
        return RuntimeBudgetSnapshot(ModelBudget(context_window_tokens=memory_policy.context_window_tokens))

    store = session_store or SessionStore()
    session_arguments = {
        "provider": restored.provider or initial_provider,
        "system_prompt": system_prompt,
        "registry": workspace_registry,
        "execution_snapshot_provider": (
            skill_runtime.snapshot if skill_runtime is not None else None
        ),
        "tool_loop_limits": (
            WORKSPACE_TOOL_LOOP_LIMITS
            if workspace_registry is not None
            else DEFAULT_TOOL_LOOP_LIMITS
        ),
        "budget_snapshot_provider": budget_snapshot,
    }
    try:
        chat_session = ChatSession.load(store, **session_arguments)
    except ChatRuntimeError as exc:
        issues.append(_issue("history", exc.user_message, blocks_prompt=True))
        chat_session = ChatSession(store, **session_arguments)

    if system_prompt_error is not None:
        issues.append(
            _issue("system_prompt", system_prompt_error, blocks_prompt=True)
        )
    if workspace_error is not None:
        issues.append(_issue("workspace", workspace_error, blocks_prompt=True))
    if skill_runtime is not None:
        skill_status = skill_runtime.status()
        skills_count = skill_status.skills_count
        skills_error = skill_status.error
    if skills_error is not None:
        issues.append(
            _issue("skills", skills_error, blocks_prompt=False)
        )

    task_store = None
    if task_policy is not None:
        try:
            task_store = TaskRunStore()
            task_store.recover_interrupted()
        except TaskRunError:
            issues.append(StartupIssue("tasks", "任务记录不可用，长任务已关闭。", IssueSeverity.WARNING, blocks_prompt=False))
            task_store = None

    return TuiDependencies(
        chat_session=chat_session,
        chat_runner=chat_session.send,
        runtime_info=current_info,
        model_selection=model_selection,
        skill_runtime=skill_runtime,
        health=TuiHealthState(tuple(issues)),
        system_prompt_loaded=chat_session.system_prompt_loaded,
        workspace_enabled=(
            workspace_registry is not None or skill_runtime is not None
        ),
        skills_count=skills_count,
        clock=clock,
        task_policy=task_policy,
        task_store=task_store,
    )


def injected_tui_dependencies(
    *,
    chat_runner: ChatRunner | None = None,
    chat_session: ChatSession | None = None,
    runtime_info: ChatRuntimeInfo,
    clock: Clock = time.monotonic,
    skill_runtime: SkillRuntime | None = None,
    skills_count: int = 0,
    skills_error: str | None = None,
    configuration_error: str | None = None,
    history_error: str | None = None,
    system_prompt_error: str | None = None,
    system_prompt_loaded: bool = False,
    workspace_error: str | None = None,
    workspace_enabled: bool = False,
    model_options: tuple[ModelOption, ...] = (),
    model_selection_store: ModelSelectionStore | None = None,
    provider_factory: ProviderFactory = create_provider_for_model,
) -> TuiDependencies:
    """为测试或宿主构造不读取环境和生产磁盘的显式依赖。"""

    if chat_runner is None:
        if chat_session is None:
            raise ValueError("chat_runner or chat_session is required")
        chat_runner = chat_session.send
    if skill_runtime is not None:
        status = skill_runtime.status()
        skills_count = status.skills_count
        skills_error = status.error
    issues = tuple(
        issue
        for issue in (
            _optional_issue(
                "configuration",
                configuration_error,
                blocks_prompt=True,
            ),
            _optional_issue("history", history_error, blocks_prompt=True),
            _optional_issue(
                "system_prompt",
                system_prompt_error,
                blocks_prompt=True,
            ),
            _optional_issue("workspace", workspace_error, blocks_prompt=True),
            _optional_issue("skills", skills_error, blocks_prompt=False),
        )
        if issue is not None
    )
    model_selection = ModelSelectionService(
        tuple(model_options),
        model_selection_store,
        provider_factory=provider_factory,
    )
    return TuiDependencies(
        chat_session=chat_session,
        chat_runner=chat_runner,
        runtime_info=runtime_info,
        model_selection=model_selection,
        skill_runtime=skill_runtime,
        health=TuiHealthState(issues),
        system_prompt_loaded=(
            chat_session.system_prompt_loaded
            if chat_session is not None
            else system_prompt_loaded
        ),
        workspace_enabled=workspace_enabled or skill_runtime is not None,
        skills_count=skills_count,
        clock=clock,
    )


def _issue(code: str, message: str, *, blocks_prompt: bool) -> StartupIssue:
    return StartupIssue(
        code,
        message,
        IssueSeverity.ERROR,
        blocks_prompt=blocks_prompt,
    )


def _optional_issue(
    code: str,
    message: str | None,
    *,
    blocks_prompt: bool,
) -> StartupIssue | None:
    if message is None:
        return None
    return _issue(code, message, blocks_prompt=blocks_prompt)
