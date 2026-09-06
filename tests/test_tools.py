import asyncio
import json
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from tools.contracts import (
    ToolApprovalRequest,
    ToolArgumentError,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolErrorCode,
    ToolExecutionContext,
    ToolPayloadLimitError,
    ToolRejectedError,
)
from tools import GetCurrentTimeTool, create_default_registry
from tools.registry import MAX_ARGUMENT_BYTES, MAX_RESULT_BYTES, ToolRegistry


class FakeTool:
    def __init__(
        self,
        name="lookup",
        *,
        result=None,
        error=None,
        events=None,
    ):
        self.definition = ToolDefinition(
            name=name,
            description="Look up a deterministic test value",
            parameters={"type": "object", "properties": {}},
        )
        self.result = {"value": "ok"} if result is None else result
        self.error = error
        self.events = events

    async def invoke(self, arguments):
        if self.events is not None:
            self.events.append((self.definition.name, dict(arguments)))
        if self.error is not None:
            raise self.error
        return self.result


class FakeApprovalTool(FakeTool):
    def __init__(self, *, events=None, max_argument_bytes=8 * 1024):
        super().__init__(name="write_file", events=events)
        self.definition = ToolDefinition(
            name="write_file",
            description="Apply a test write",
            parameters={"type": "object", "properties": {}},
            effect=ToolEffect.MUTATING,
            max_argument_bytes=max_argument_bytes,
        )

    async def preview(self, call_id, arguments):
        if self.events is not None:
            self.events.append(("preview", call_id, dict(arguments)))
        return ToolApprovalRequest(
            call_id=call_id,
            tool_name=self.definition.name,
            title="Apply test write",
            paths=("app/example.py",),
            diff_text="--- a/app/example.py\n+++ b/app/example.py\n",
            fingerprint="a" * 64,
        )

    async def invoke(self, arguments):
        if self.events is not None:
            self.events.append(("invoke", dict(arguments)))
        return {"changed": True}


@pytest.mark.parametrize(
    "tool",
    [
        FakeTool(name="bad name"),
        FakeTool(name="x" * 65),
    ],
)
def test_registry_rejects_invalid_tool_names(tool):
    with pytest.raises(ValueError):
        ToolRegistry([tool])


def test_registry_rejects_empty_duplicate_and_invalid_definition():
    with pytest.raises(ValueError):
        ToolRegistry([])
    with pytest.raises(ValueError):
        ToolRegistry([FakeTool(), FakeTool()])

    invalid = FakeTool()
    invalid.definition = ToolDefinition("lookup", " ", {"type": "object"})
    with pytest.raises(ValueError):
        ToolRegistry([invalid])

    invalid.definition = ToolDefinition("lookup", "valid", {"type": "array"})
    with pytest.raises(ValueError):
        ToolRegistry([invalid])


def test_registry_accepts_mapping_parameter_schema():
    tool = FakeTool()
    tool.definition = ToolDefinition(
        "lookup",
        "valid",
        MappingProxyType({"type": "object", "properties": {}}),
    )

    assert ToolRegistry([tool]).definitions == (tool.definition,)


def test_registry_rejects_undeclared_required_parameter():
    tool = FakeTool()
    tool.definition = ToolDefinition(
        "lookup",
        "valid",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name", "missing"],
            "additionalProperties": False,
        },
    )

    with pytest.raises(ValueError):
        ToolRegistry([tool])


def test_registry_rejects_invalid_effect_limit_and_mutating_tool_without_preview():
    invalid_effect = FakeTool()
    invalid_effect.definition = ToolDefinition(
        "lookup",
        "valid",
        {"type": "object"},
        effect="write",  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError):
        ToolRegistry([invalid_effect])

    invalid_limit = FakeTool()
    invalid_limit.definition = ToolDefinition(
        "lookup",
        "valid",
        {"type": "object"},
        max_argument_bytes=0,
    )
    with pytest.raises(ValueError):
        ToolRegistry([invalid_limit])

    invalid_result_limit = FakeTool()
    invalid_result_limit.definition = ToolDefinition(
        "lookup",
        "valid",
        {"type": "object"},
        max_result_bytes=0,
    )
    with pytest.raises(ValueError):
        ToolRegistry([invalid_result_limit])

    missing_preview = FakeTool()
    missing_preview.definition = ToolDefinition(
        "lookup",
        "valid",
        {"type": "object"},
        effect=ToolEffect.MUTATING,
    )
    with pytest.raises(ValueError):
        ToolRegistry([missing_preview])

    missing_execution_preview = FakeTool()
    missing_execution_preview.definition = ToolDefinition(
        "lookup",
        "valid",
        {"type": "object"},
        effect=ToolEffect.EXECUTING,
    )
    with pytest.raises(ValueError):
        ToolRegistry([missing_execution_preview])

@pytest.mark.parametrize("arguments_json", ["not-json", "[]", '"text"'])
def test_registry_returns_safe_error_for_invalid_arguments(arguments_json):
    registry = ToolRegistry([FakeTool()])

    result = asyncio.run(
        registry.execute(ToolCall("call-1", "lookup", arguments_json))
    )

    assert result.is_error is True
    assert json.loads(result.output) == {
        "ok": False,
        "error": {
            "code": "invalid_arguments",
            "message": "Tool arguments are invalid",
        },
    }


def test_registry_never_executes_unknown_tool():
    events = []
    registry = ToolRegistry([FakeTool(events=events)])

    result = asyncio.run(
        registry.execute(ToolCall("call-2", "missing", '{"value":1}'))
    )

    assert json.loads(result.output)["error"]["code"] == "unknown_tool"
    assert events == []


def test_registry_requires_approval_before_mutating_tool_execution():
    events = []
    tool = FakeApprovalTool(events=events)
    registry = ToolRegistry([tool])

    unavailable = asyncio.run(
        registry.execute(ToolCall("call-write", "write_file", "{}"))
    )
    assert json.loads(unavailable.output)["error"]["code"] == (
        "approval_unavailable"
    )
    assert events == [("preview", "call-write", {})]

    async def approve(request):
        events.append(("approval", request.fingerprint))
        return True

    approved = asyncio.run(
        registry.execute(
            ToolCall("call-write", "write_file", "{}"),
            ToolExecutionContext(approval_handler=approve),
        )
    )

    assert json.loads(approved.output)["data"] == {"changed": True}
    assert events[-3:] == [
        ("preview", "call-write", {}),
        ("approval", "a" * 64),
        ("invoke", {}),
    ]


def test_registry_rejects_and_deduplicates_same_mutation_approval():
    events = []
    approvals = []

    async def reject(request):
        approvals.append(request.fingerprint)
        return False

    context = ToolExecutionContext(approval_handler=reject)
    registry = ToolRegistry([FakeApprovalTool(events=events)])
    call = ToolCall("call-write", "write_file", "{}")

    first = asyncio.run(registry.execute(call, context))
    second = asyncio.run(registry.execute(call, context))

    assert json.loads(first.output)["error"]["code"] == "approval_denied"
    assert json.loads(second.output)["error"]["code"] == "approval_denied"
    assert approvals == ["a" * 64]
    assert all(event[0] != "invoke" for event in events)


def test_registry_hides_approval_failure_and_propagates_cancellation():
    registry = ToolRegistry([FakeApprovalTool()])
    call = ToolCall("call-write", "write_file", "{}")

    async def fail(_request):
        raise RuntimeError("sensitive approval failure")

    failed = asyncio.run(
        registry.execute(call, ToolExecutionContext(approval_handler=fail))
    )
    assert json.loads(failed.output)["error"]["code"] == "approval_unavailable"
    assert "sensitive" not in failed.output

    async def cancel(_request):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            registry.execute(
                call,
                ToolExecutionContext(approval_handler=cancel),
            )
        )


@pytest.mark.parametrize(
    "error, expected_code",
    [
        (ToolArgumentError("sensitive argument detail"), "invalid_arguments"),
        (RuntimeError("sensitive internal detail"), "execution_failed"),
    ],
)
def test_registry_hides_tool_exception_details(error, expected_code):
    registry = ToolRegistry([FakeTool(error=error)])

    result = asyncio.run(
        registry.execute(ToolCall("call-3", "lookup", "{}"))
    )

    payload = json.loads(result.output)
    assert payload["error"]["code"] == expected_code
    assert "sensitive" not in result.output


def test_registry_maps_explicit_tool_rejection_to_safe_error():
    registry = ToolRegistry(
        [FakeTool(error=ToolRejectedError(ToolErrorCode.PROTECTED_PATH))]
    )

    result = asyncio.run(registry.execute(ToolCall("call-safe", "lookup", "{}")))

    assert json.loads(result.output)["error"] == {
        "code": "protected_path",
        "message": "Workspace path is protected",
    }


def test_registry_rejects_non_standard_json_result_as_execution_failure():
    registry = ToolRegistry([FakeTool(result=float("nan"))])

    result = asyncio.run(
        registry.execute(ToolCall("call-json", "lookup", "{}"))
    )

    assert json.loads(result.output)["error"]["code"] == "execution_failed"


def test_registry_executes_calls_serially_and_keeps_result_order():
    events = []
    registry = ToolRegistry(
        [
            FakeTool("first", result={"order": 1}, events=events),
            FakeTool("second", result={"order": 2}, events=events),
        ]
    )
    calls = (
        ToolCall("call-a", "first", '{"value":"甲"}'),
        ToolCall("call-b", "second", '{"value":"乙"}'),
    )

    results = asyncio.run(registry.execute_all(calls))

    assert events == [("first", {"value": "甲"}), ("second", {"value": "乙"})]
    assert [result.call_id for result in results] == ["call-a", "call-b"]
    assert [json.loads(result.output)["data"]["order"] for result in results] == [
        1,
        2,
    ]


def test_registry_applies_utf8_argument_and_result_byte_limits():
    registry = ToolRegistry([FakeTool()])
    oversized_arguments = "你" * (MAX_ARGUMENT_BYTES // 3 + 1)

    with pytest.raises(ToolPayloadLimitError):
        asyncio.run(
            registry.execute(
                ToolCall("call-4", "lookup", oversized_arguments)
            )
        )

    oversized_result = "你" * (MAX_RESULT_BYTES // 3 + 1)
    result = asyncio.run(
        ToolRegistry([FakeTool(result=oversized_result)]).execute(
            ToolCall("call-5", "lookup", "{}")
        )
    )
    assert json.loads(result.output)["error"]["code"] == "result_too_large"
    assert oversized_result not in result.output


def test_registry_applies_per_tool_argument_limit():
    registry = ToolRegistry([FakeApprovalTool(max_argument_bytes=16 * 1024)])
    arguments = json.dumps({"text": "你" * 3000}, ensure_ascii=False)

    result = asyncio.run(
        registry.execute(
            ToolCall("call-large", "write_file", arguments),
            ToolExecutionContext(approval_handler=lambda _request: None),
        )
    )

    assert json.loads(result.output)["error"]["code"] == "approval_unavailable"


def test_registry_does_not_swallow_cancellation():
    registry = ToolRegistry([FakeTool(error=asyncio.CancelledError())])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(registry.execute(ToolCall("call-6", "lookup", "{}")))


def test_get_current_time_returns_deterministic_localized_iso_time():
    fixed_utc = datetime(2026, 8, 8, 4, 30, 15, tzinfo=timezone.utc)
    registry = ToolRegistry([GetCurrentTimeTool(clock=lambda: fixed_utc)])

    result = asyncio.run(
        registry.execute(
            ToolCall(
                "time-1",
                "get_current_time",
                '{"timezone":"Asia/Shanghai"}',
            )
        )
    )

    assert json.loads(result.output) == {
        "ok": True,
        "data": {
            "timezone": "Asia/Shanghai",
            "datetime": "2026-08-08T12:30:15+08:00",
        },
    }


@pytest.mark.parametrize(
    "arguments_json",
    [
        "{}",
        '{"timezone":""}',
        '{"timezone":123}',
        '{"timezone":"Unknown/Nowhere"}',
        '{"timezone":"UTC","extra":true}',
    ],
)
def test_get_current_time_rejects_invalid_arguments(arguments_json):
    registry = ToolRegistry([GetCurrentTimeTool()])

    result = asyncio.run(
        registry.execute(
            ToolCall("time-2", "get_current_time", arguments_json)
        )
    )

    assert json.loads(result.output)["error"]["code"] == "invalid_arguments"


def test_default_registry_only_exposes_get_current_time():
    registry = create_default_registry()

    assert [definition.name for definition in registry.definitions] == [
        "get_current_time"
    ]
