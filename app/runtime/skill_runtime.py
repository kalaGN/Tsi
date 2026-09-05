"""为 TUI 在请求边界发布不可变 Skill 与工具执行快照。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.runtime.session import ChatExecutionSnapshot
from app.runtime.system_prompt import compose_system_prompt
from tools.skill_installation import (
    InstallSkillTool,
    SkillInstaller,
    SkillSourceFetcher,
)
from tools.skills import SkillCatalog
from tools.registry import ToolRegistry
from tools.workspace import (
    WorkspaceChangeJournal,
    WorkspacePolicy,
    create_workspace_registry,
)


@dataclass(frozen=True)
class SkillRuntimeStatus:
    """TUI 状态栏可读取且不暴露 Skill 正文的运行状态。"""

    version: int
    skills_count: int
    error: str | None


@dataclass(frozen=True)
class AvailableSkill:
    """供 TUI 本地命令展示的有限 Skill 元数据。"""

    name: str
    description: str
    relative_entrypoint: str


class SkillRuntime:
    """组合 Catalog、安装事务和每次发送使用的 Registry。"""

    def __init__(
        self,
        startup_directory: Path,
        agents_prompt: str | None,
        workspace_policy: WorkspacePolicy,
        initial_catalog: SkillCatalog | None,
        *,
        initial_error: str | None = None,
        codex_skills_root: Path | None = None,
        github_fetcher: SkillSourceFetcher | None = None,
        journal: WorkspaceChangeJournal | None = None,
        registry_factory: Callable[..., ToolRegistry] = create_workspace_registry,
    ) -> None:
        self._startup_directory = Path(startup_directory)
        self._agents_prompt = agents_prompt
        self._workspace_policy = workspace_policy
        self._catalog = initial_catalog
        self._error = initial_error
        self._version = 1
        self._journal = journal or WorkspaceChangeJournal()
        self._registry_factory = registry_factory
        installer = SkillInstaller(
            self._startup_directory,
            self.publish,
            codex_skills_root=codex_skills_root,
            github_fetcher=github_fetcher,
        )
        self._install_tool = InstallSkillTool(installer)

    def status(self) -> SkillRuntimeStatus:
        """返回当前内存版本；不会为状态栏重新扫描磁盘。"""

        return SkillRuntimeStatus(
            version=self._version,
            skills_count=self._catalog.count if self._catalog is not None else 0,
            error=self._error,
        )

    def available_skills(self) -> tuple[AvailableSkill, ...]:
        """返回已发布 Catalog 的稳定摘要，不重新读取项目文件。"""

        if self._catalog is None:
            return ()
        return tuple(
            AvailableSkill(
                name=skill.name,
                description=skill.description,
                relative_entrypoint=skill.relative_entrypoint,
            )
            for _, skill in sorted(self._catalog.skills.items())
        )

    def publish(self, catalog: SkillCatalog) -> int:
        """只在完整 Catalog 已生成后一次发布下一版本。"""

        if not isinstance(catalog, SkillCatalog):
            raise TypeError("catalog is invalid")
        self._catalog = catalog
        self._error = None
        self._version += 1
        return self._version

    def snapshot(self) -> ChatExecutionSnapshot:
        """为下一次发送固定当前 Catalog、prompt 和 Registry。"""

        skill_prompt = self._catalog.prompt if self._catalog is not None else None
        registry = self._registry_factory(
            self._workspace_policy,
            journal=self._journal,
            skill_catalog=self._catalog,
            install_skill_tool=self._install_tool,
        )
        return ChatExecutionSnapshot(
            system_prompt=compose_system_prompt(self._agents_prompt, skill_prompt),
            registry=registry,
            version=self._version,
        )
