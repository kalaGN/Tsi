"""默认工具、Workspace 工具契约与项目 Skill 的稳定公共入口。"""

from tools.builtin import GetCurrentTimeTool
from tools.contracts import (
    AnyToolApprovalRequest,
    GIT_APPROVAL_WARNING_TEXT,
    GitApprovalRequest,
    SKILL_INSTALL_APPROVAL_WARNING_TEXT,
    SCRIPT_APPROVAL_WARNING_TEXT,
    ScriptApprovalRequest,
    SkillInstallApprovalRequest,
    ToolApprovalHandler,
    ToolApprovalRequest,
    Tool,
    ToolArgumentError,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolPayloadLimitError,
    ToolResult,
    ToolResultHandler,
    ToolRuntime,
)
from tools.registry import ToolRegistry
from tools.groups import (
    ActivateToolGroupsTool,
    GroupedToolRegistry,
    ToolGroup,
    ToolGroupDefinition,
)
from tools.git import GitCommitTool, GitPushTool, GitStageTool
from tools.skills import (
    LoadSkillTool,
    ReadSkillResourceTool,
    RunSkillScriptTool,
    SkillCatalog,
    SkillLoadError,
    load_skill_catalog,
)


def create_default_registry() -> ToolRegistry:
    """创建只包含项目默认只读工具的独立 Registry。"""

    return ToolRegistry((GetCurrentTimeTool(),))


__all__ = [
    "GetCurrentTimeTool",
    "AnyToolApprovalRequest",
    "GIT_APPROVAL_WARNING_TEXT",
    "GitApprovalRequest",
    "ScriptApprovalRequest",
    "SkillInstallApprovalRequest",
    "SCRIPT_APPROVAL_WARNING_TEXT",
    "SKILL_INSTALL_APPROVAL_WARNING_TEXT",
    "LoadSkillTool",
    "ReadSkillResourceTool",
    "RunSkillScriptTool",
    "SkillCatalog",
    "SkillLoadError",
    "Tool",
    "ToolApprovalHandler",
    "ToolApprovalRequest",
    "ToolArgumentError",
    "ToolCall",
    "ToolDefinition",
    "ToolEffect",
    "ToolExecutionContext",
    "ToolPayloadLimitError",
    "ToolRegistry",
    "ActivateToolGroupsTool",
    "GroupedToolRegistry",
    "GitStageTool",
    "GitCommitTool",
    "GitPushTool",
    "ToolGroup",
    "ToolGroupDefinition",
    "ToolResult",
    "ToolResultHandler",
    "ToolRuntime",
    "create_default_registry",
    "load_skill_catalog",
]
