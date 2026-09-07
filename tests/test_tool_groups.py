import asyncio
import json

from tools.contracts import (
    ToolApprovalRequest,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
)
from tools.groups import GroupedToolRegistry, ToolGroup, ToolGroupDefinition


class ReadTool:
    definition = ToolDefinition(
        "read_demo",
        "读取测试数据",
        {"type": "object", "properties": {}},
    )

    async def invoke(self, arguments):
        return {"value": "ok"}


class WriteTool:
    definition = ToolDefinition(
        "write_demo",
        "写入测试数据",
        {"type": "object", "properties": {}},
        effect=ToolEffect.MUTATING,
    )

    async def preview(self, call_id, arguments):
        return ToolApprovalRequest(
            call_id,
            self.definition.name,
            "确认测试写入",
            ("demo.txt",),
            "--- a/demo.txt\n+++ b/demo.txt\n@@ -0,0 +1 @@\n+ok\n",
            "a" * 64,
        )

    async def invoke(self, arguments):
        return {"written": True}


def create_registry(*, preactivated=(), max_expansions=2):
    return GroupedToolRegistry(
        (ReadTool(), WriteTool()),
        (
            ToolGroupDefinition(
                ToolGroup.GENERAL,
                "读取测试数据",
                ("read_demo",),
            ),
            ToolGroupDefinition(
                ToolGroup.SKILL_INSTALL,
                "执行测试写入",
                ("write_demo",),
            ),
        ),
        preactivated_groups=preactivated,
        max_expansions=max_expansions,
    )


def execute(registry, name, arguments, context=None):
    result = asyncio.run(
        registry.execute(
            ToolCall("call-1", name, json.dumps(arguments)),
            context,
        )
    )
    return json.loads(result.output), result


def test_grouped_registry_initially_exposes_only_activation_tool():
    registry = create_registry()

    assert tuple(item.name for item in registry.definitions) == (
        "activate_tool_groups",
    )
    output, result = execute(registry, "read_demo", {})
    assert result.is_error is True
    assert output["error"]["code"] == "unknown_tool"


def test_activation_expands_in_catalog_order_and_repeat_is_idempotent():
    registry = create_registry()

    output, result = execute(
        registry,
        "activate_tool_groups",
        {"groups": ["skill_install", "general"]},
    )
    repeated, _ = execute(
        registry,
        "activate_tool_groups",
        {"groups": ["general"]},
    )

    assert result.is_error is False
    assert output["data"]["changed"] is True
    assert output["data"]["activations_remaining"] == 1
    assert tuple(item.name for item in registry.definitions) == (
        "activate_tool_groups",
        "read_demo",
        "write_demo",
    )
    assert repeated["data"]["changed"] is False
    assert repeated["data"]["activations_remaining"] == 1


def test_activation_rejects_invalid_unavailable_and_third_expansion_atomically():
    registry = create_registry(max_expansions=1)

    invalid, _ = execute(
        registry,
        "activate_tool_groups",
        {"groups": ["general", "general"]},
    )
    unavailable, _ = execute(
        registry,
        "activate_tool_groups",
        {"groups": ["workspace_read"]},
    )
    execute(registry, "activate_tool_groups", {"groups": ["general"]})
    limited, _ = execute(
        registry,
        "activate_tool_groups",
        {"groups": ["skill_install"]},
    )

    assert invalid["error"]["code"] == "invalid_arguments"
    assert unavailable["error"]["code"] == "tool_group_unavailable"
    assert limited["error"]["code"] == "tool_group_limit"
    assert tuple(item.name for item in registry.definitions) == (
        "activate_tool_groups",
        "read_demo",
    )


def test_preactivation_is_free_and_write_tool_still_requires_approval():
    registry = create_registry(preactivated=("skill_install",))
    denied, denied_result = execute(registry, "write_demo", {})

    async def approve(_request):
        return True

    approved, approved_result = execute(
        registry,
        "write_demo",
        {},
        ToolExecutionContext(approval_handler=approve),
    )
    activated, _ = execute(
        registry,
        "activate_tool_groups",
        {"groups": ["general"]},
    )

    assert denied_result.is_error is True
    assert denied["error"]["code"] == "approval_unavailable"
    assert approved_result.is_error is False
    assert approved["data"]["written"] is True
    assert activated["data"]["activations_remaining"] == 1
