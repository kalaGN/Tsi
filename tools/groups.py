"""请求级工具组和模型驱动的渐进式工具披露。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from tools.contracts import (
    Tool,
    ToolArgumentError,
    ToolCall,
    ToolDefinition,
    ToolErrorCode,
    ToolExecutionContext,
    ToolRejectedError,
    ToolResult,
)
from tools.registry import ToolRegistry


class ToolGroup(str, Enum):
    """宿主允许模型请求的固定能力组。"""

    GENERAL = "general"
    WORKSPACE_READ = "workspace_read"
    WORKSPACE_WRITE = "workspace_write"
    SKILLS = "skills"
    SKILL_INSTALL = "skill_install"


@dataclass(frozen=True)
class ToolGroupDefinition:
    """一个工具组的用途说明和宿主工具白名单。"""

    group: ToolGroup
    description: str
    tool_names: tuple[str, ...]


class ActivateToolGroupsTool:
    """让模型按当前任务需要激活一个或多个宿主工具组。"""

    def __init__(self, registry: "GroupedToolRegistry") -> None:
        self._registry = registry
        groups = [
            definition.group.value for definition in registry.group_definitions
        ]
        details = "; ".join(
            f"{definition.group.value}: {definition.description}"
            for definition in registry.group_definitions
        )
        self.definition = ToolDefinition(
            "activate_tool_groups",
            f"按任务需要激活工具组。可用组：{details}",
            {
                "type": "object",
                "properties": {
                    "groups": {
                        "type": "array",
                        "items": {"type": "string", "enum": groups},
                        "minItems": 1,
                        "maxItems": 5,
                    }
                },
                "required": ["groups"],
                "additionalProperties": False,
            },
        )

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        if set(arguments) != {"groups"}:
            raise ToolArgumentError()
        requested = arguments.get("groups")
        if (
            not isinstance(requested, list)
            or not 1 <= len(requested) <= 5
            or any(not isinstance(group, str) for group in requested)
            or len(set(requested)) != len(requested)
        ):
            raise ToolArgumentError()
        try:
            groups = tuple(ToolGroup(group) for group in requested)
        except ValueError as exc:
            raise ToolArgumentError() from exc
        return self._registry.activate(groups)


class GroupedToolRegistry:
    """只执行当前已披露工具，并在请求内原子扩展可见集合。"""

    def __init__(
        self,
        tools: Sequence[Tool],
        groups: Sequence[ToolGroupDefinition],
        *,
        preactivated_groups: Sequence[ToolGroup | str] = (),
        max_expansions: int = 2,
    ) -> None:
        catalog = tuple(tools)
        # 复用普通 Registry 的名称、Schema 和审批契约验证。
        ToolRegistry(catalog)
        if type(max_expansions) is not int or max_expansions < 0:
            raise ValueError("max expansions is invalid")

        by_name = {tool.definition.name: tool for tool in catalog}
        normalized_groups = tuple(groups)
        if not normalized_groups:
            raise ValueError("at least one tool group is required")
        group_map: dict[ToolGroup, ToolGroupDefinition] = {}
        for definition in normalized_groups:
            if (
                not isinstance(definition, ToolGroupDefinition)
                or not isinstance(definition.group, ToolGroup)
                or not isinstance(definition.description, str)
                or not definition.description.strip()
                or not isinstance(definition.tool_names, tuple)
                or not definition.tool_names
                or any(not isinstance(name, str) for name in definition.tool_names)
                or len(set(definition.tool_names)) != len(definition.tool_names)
                or any(name not in by_name for name in definition.tool_names)
                or definition.group in group_map
            ):
                raise ValueError("tool group definition is invalid")
            group_map[definition.group] = definition
        read = group_map.get(ToolGroup.WORKSPACE_READ)
        write = group_map.get(ToolGroup.WORKSPACE_WRITE)
        if read is not None and write is not None and not set(read.tool_names) <= set(
            write.tool_names
        ):
            raise ValueError("workspace write group must include read tools")

        initial: set[ToolGroup] = set()
        for raw_group in preactivated_groups:
            try:
                group = (
                    raw_group
                    if isinstance(raw_group, ToolGroup)
                    else ToolGroup(raw_group)
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("preactivated tool group is invalid") from exc
            if group not in group_map:
                raise ValueError("preactivated tool group is unavailable")
            initial.add(group)

        self._catalog = catalog
        self._group_map = group_map
        self._active_groups = initial
        self._max_expansions = max_expansions
        self._expansions = 0
        self._activation_tool = ActivateToolGroupsTool(self)
        self._active_registry = self._build_registry()

    @property
    def group_definitions(self) -> tuple[ToolGroupDefinition, ...]:
        return tuple(self._group_map.values())

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._active_registry.definitions

    @property
    def active_groups(self) -> tuple[ToolGroup, ...]:
        return tuple(
            group for group in self._group_map if group in self._active_groups
        )

    async def execute(
        self,
        call: ToolCall,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        """通过当前可见 Registry 执行，隐藏工具仍返回 unknown_tool。"""

        registry = self._active_registry
        return await registry.execute(call, context)

    def activate(self, groups: tuple[ToolGroup, ...]) -> dict[str, object]:
        """原子追加工具组；重复激活不消耗扩展次数。"""

        if any(group not in self._group_map for group in groups):
            raise ToolRejectedError(ToolErrorCode.TOOL_GROUP_UNAVAILABLE)
        additions = set(groups) - self._active_groups
        if additions:
            if self._expansions >= self._max_expansions:
                raise ToolRejectedError(ToolErrorCode.TOOL_GROUP_LIMIT)
            next_active_groups = self._active_groups | additions
            next_registry = self._build_registry(next_active_groups)
            self._active_groups = next_active_groups
            self._expansions += 1
            self._active_registry = next_registry
        return {
            "active_groups": [group.value for group in self.active_groups],
            "available_tools": [item.name for item in self.definitions],
            "changed": bool(additions),
            "activations_remaining": self._max_expansions - self._expansions,
        }

    def _build_registry(
        self,
        active_groups: set[ToolGroup] | None = None,
    ) -> ToolRegistry:
        selected_groups = (
            self._active_groups if active_groups is None else active_groups
        )
        active_names = {
            name
            for group in selected_groups
            for name in self._group_map[group].tool_names
        }
        visible = [self._activation_tool]
        visible.extend(
            tool for tool in self._catalog if tool.definition.name in active_names
        )
        return ToolRegistry(tuple(visible))
