"""TUI 底部运行状态的格式化与展示。"""

from dataclasses import dataclass

from textual.widgets import Static

from app.tui.state import RunStatus


@dataclass(frozen=True)
class StatusBarState:
    """状态栏一次绘制所需的有限状态快照。"""

    provider: str
    model: str
    api_key_configured: bool
    system_prompt_loaded: bool
    system_prompt_error: str | None
    workspace_enabled: bool
    workspace_error: str | None
    skills_count: int
    skills_error: str | None
    run_status: RunStatus


class StatusBar(Static):
    """把运行状态转换为稳定、无敏感正文的单行摘要。"""

    def __init__(self) -> None:
        super().__init__(id="status-bar", markup=False)

    def show_status(self, state: StatusBarState) -> None:
        """根据状态快照更新底部信息。"""

        key_status = "configured" if state.api_key_configured else "missing"
        if state.system_prompt_error is not None:
            agents_status = "error"
        elif state.system_prompt_loaded:
            agents_status = "loaded"
        else:
            agents_status = "none"
        if state.workspace_error is not None:
            workspace_status = "error"
        elif state.workspace_enabled:
            workspace_status = "enabled"
        else:
            workspace_status = "disabled"
        skills_status = "error" if state.skills_error else state.skills_count
        self.update(
            f"{_provider_display_name(state.provider)} | {state.model} | "
            f"Key: {key_status} | AGENTS: {agents_status} | "
            f"Workspace: {workspace_status} | Skills: {skills_status} | "
            f"{state.run_status.value}"
        )


def _provider_display_name(provider: str) -> str:
    """保留 DeepSeek 品牌大小写，其余名称使用常规标题格式。"""

    return "DeepSeek" if provider == "deepseek" else provider.title()
