"""Web/TUI 共用的固定工具能力目录；披露不等于授权。"""

from collections.abc import Sequence

from tools.groups import ToolGroup, ToolGroupDefinition


WORKSPACE_READ_NAMES = (
    "list_workspace_files",
    "search_workspace_text",
    "read_workspace_files",
    "read_workspace_file",
    "get_workspace_git_status",
    "get_workspace_git_diff",
)
WORKSPACE_WRITE_NAMES = WORKSPACE_READ_NAMES + (
    "apply_workspace_edits",
    "delete_workspace_file",
    "run_project_check",
    "undo_workspace_change",
)


def allowed_tool_groups(
    available_names: set[str], *, entry: str, mcp_names: Sequence[str] = (),
) -> tuple[ToolGroupDefinition, ...]:
    """按入口固定白名单构造目录；Registry 仍独立校验每次调用与审批。"""

    if entry not in {"web", "tui", "readonly"}:
        raise ValueError("invalid tool entry")
    groups = [
        ToolGroupDefinition(ToolGroup.GENERAL, "读取指定时区的当前时间", ("get_current_time",)),
    ]
    if entry == "web" and "web_search" in available_names:
        groups.append(ToolGroupDefinition(
            ToolGroup.WEB_SEARCH, "搜索需要实时或项目外部信息的公开网络内容", ("web_search",),
        ))
    groups.append(ToolGroupDefinition(
        ToolGroup.WORKSPACE_READ,
        "浏览工作区；已知多个关键词时一次搜索，多个文件时一次批量读取；也可查看 Git 状态或差异",
        WORKSPACE_READ_NAMES,
    ))
    if entry != "readonly":
        groups.append(ToolGroupDefinition(
            ToolGroup.WORKSPACE_WRITE,
            "读取并修改工作区、运行检查和撤销本轮修改",
            WORKSPACE_WRITE_NAMES,
        ))
    if entry == "tui":
        if "load_skill" in available_names:
            groups.append(ToolGroupDefinition(
                ToolGroup.SKILLS, "加载 Skill 指令、读取资源并按审批运行脚本",
                ("load_skill", "read_skill_resource", "run_skill_script"),
            ))
        if "install_skill" in available_names:
            groups.append(ToolGroupDefinition(
                ToolGroup.SKILL_INSTALL, "从受支持来源安装 Skill", ("install_skill",),
            ))
        groups.append(ToolGroupDefinition(
            ToolGroup.GIT_WRITE, "经逐次审批暂存文件、创建中文提交并推送既有上游",
            ("git_stage", "git_commit", "git_push"),
        ))
    if entry != "readonly" and mcp_names:
        groups.append(ToolGroupDefinition(
            ToolGroup.MCP, "调用用户配置的外部 MCP Server 工具，每次调用需本地审批",
            tuple(mcp_names),
        ))
    if any(not set(group.tool_names) <= available_names for group in groups):
        raise ValueError("tool policy references unavailable tool")
    return tuple(groups)
