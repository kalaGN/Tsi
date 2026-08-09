import asyncio
import json
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from tools.contracts import (
    ToolArgumentError,
    ToolCall,
    ToolDefinition,
    ToolPayloadLimitError,
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
