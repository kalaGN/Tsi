"""`python -m app.tui` 的终端启动入口。"""

import os
from pathlib import Path

from dotenv import load_dotenv

from app.observability.model_logging import configure_model_logging
from app.runtime.model_selection_store import ModelSelectionStore
from app.runtime.model_config_store import MODEL_ENV_KEYS, ModelConfigError, ModelConfigStore
from app.runtime.chat import ChatRuntimeError
from app.runtime.system_prompt import (
    SystemPromptLoadError,
    compose_system_prompt,
    load_system_prompt,
)
from app.runtime.skill_runtime import SkillRuntime
from app.tui.bootstrap import TuiDependencies, build_tui_dependencies
from app.services.llm.factory import create_provider, create_provider_for_model
from tools.skills import SkillLoadError, load_skill_catalog
from tools.workspace import WorkspacePolicy, create_intent_workspace_registry


def _create_app(
    *,
    dependencies: TuiDependencies,
):
    """延迟导入 Textual，确保终端兼容配置先于框架初始化生效。"""

    from app.tui.application import ChatTuiApp

    return ChatTuiApp(dependencies)


def main() -> None:
    """加载项目环境并启动本地 TUI。"""

    startup_directory = Path.cwd()
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)
    # Report-all-keys 会干扰部分 macOS 中文输入法，因此项目不启用该协议。
    os.environ["TEXTUAL_DISABLE_KITTY_KEY"] = "1"
    # TUI 独占终端画面，模型日志只落本地文件，避免 stderr 覆盖输入区域。
    configure_model_logging(enable_stream=False)
    try:
        agents_prompt = load_system_prompt(startup_directory)
        system_prompt_error = None
    except SystemPromptLoadError as exc:
        agents_prompt = None
        system_prompt_error = str(exc)
    try:
        skill_catalog = load_skill_catalog(startup_directory)
        skills_error = None
    except SkillLoadError as exc:
        skill_catalog = None
        skills_error = str(exc)
    try:
        workspace_policy = WorkspacePolicy(startup_directory)
        skill_runtime = SkillRuntime(
            startup_directory,
            agents_prompt,
            workspace_policy,
            skill_catalog,
            initial_error=skills_error,
            registry_factory=create_intent_workspace_registry,
        )
        initial_snapshot = skill_runtime.snapshot()
        workspace_registry = initial_snapshot.registry
        workspace_error = None
        skill_status = skill_runtime.status()
        skills_count = skill_status.skills_count
        system_prompt = initial_snapshot.system_prompt
    except (OSError, ValueError, ChatRuntimeError) as exc:
        workspace_registry = None
        skill_runtime = None
        workspace_error = exc.user_message if isinstance(exc, ChatRuntimeError) else "Workspace tools are unavailable"
        skills_count = 0
        system_prompt = compose_system_prompt(agents_prompt, None)
    model_store = ModelConfigStore()
    try:
        model_config = model_store.load()
        model_config_error = None
    except ModelConfigError:
        model_config = model_store.defaults()
        model_config_error = "模型配置无法读取，请在 Web 设置中检查本机配置文件。"
    model_values = model_config.environment()
    # 其他配置仍可来自环境；模型字段只能由私有配置文件提供。
    effective_values = {key: value for key, value in os.environ.items() if key not in MODEL_ENV_KEYS}
    effective_values.update(model_values)
    dependencies = build_tui_dependencies(
        system_prompt=system_prompt,
        system_prompt_error=system_prompt_error,
        workspace_registry=workspace_registry,
        workspace_error=workspace_error,
        skills_count=skills_count,
        skills_error=skills_error,
        skill_runtime=skill_runtime,
        model_selection_store=ModelSelectionStore(),
        model_options=model_config.options(),
        provider_factory=lambda name, model: create_provider_for_model(name, model, model_values),
        initial_provider=create_provider(model_values),
        model_config_error=model_config_error,
        environ=effective_values,
    )
    _create_app(dependencies=dependencies).run()


if __name__ == "__main__":
    main()
