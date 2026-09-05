"""`python -m app.tui` 的终端启动入口。"""

import os
from pathlib import Path

from dotenv import load_dotenv

from app.observability.model_logging import configure_model_logging
from app.runtime.system_prompt import (
    SystemPromptLoadError,
    compose_system_prompt,
    load_system_prompt,
)
from app.runtime.skill_runtime import SkillRuntime
from tools.skills import SkillLoadError, load_skill_catalog
from tools.workspace import WorkspacePolicy, create_workspace_registry


def _create_app(
    *,
    system_prompt: str | None,
    system_prompt_error: str | None,
    workspace_registry,
    workspace_error: str | None,
    skills_count: int,
    skills_error: str | None,
    skill_runtime: SkillRuntime | None = None,
):
    """延迟导入 Textual，确保终端兼容配置先于框架初始化生效。"""

    from app.tui.application import ChatTuiApp

    return ChatTuiApp(
        system_prompt=system_prompt,
        system_prompt_error=system_prompt_error,
        workspace_registry=workspace_registry,
        workspace_error=workspace_error,
        skills_count=skills_count,
        skills_error=skills_error,
        skill_runtime=skill_runtime,
    )


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
            registry_factory=create_workspace_registry,
        )
        initial_snapshot = skill_runtime.snapshot()
        workspace_registry = initial_snapshot.registry
        workspace_error = None
        skill_status = skill_runtime.status()
        skills_count = skill_status.skills_count
        system_prompt = initial_snapshot.system_prompt
    except (OSError, ValueError):
        workspace_registry = None
        skill_runtime = None
        workspace_error = "Workspace tools are unavailable"
        skills_count = 0
        system_prompt = compose_system_prompt(agents_prompt, None)
    _create_app(
        system_prompt=system_prompt,
        system_prompt_error=system_prompt_error,
        workspace_registry=workspace_registry,
        workspace_error=workspace_error,
        skills_count=skills_count,
        skills_error=skills_error,
        skill_runtime=skill_runtime,
    ).run()


if __name__ == "__main__":
    main()
