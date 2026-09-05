"""TUI 工作区变更追踪器测试。"""

import json

from app.tui.workspace_changes import AppliedChangeTracker
from tools import ToolCall, ToolResult


def _call(name: str, call_id: str = "call-1") -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments_json="{}")


def _result(
    change_id: str,
    paths: list[str],
    *,
    call_id: str = "call-1",
) -> ToolResult:
    return ToolResult(
        call_id=call_id,
        output=json.dumps(
            {"data": {"change_id": change_id, "paths": paths}},
            ensure_ascii=False,
        ),
    )


def test_tracker_collects_unique_sorted_paths() -> None:
    tracker = AppliedChangeTracker()

    tracker.observe(
        _call("apply_workspace_edits"),
        _result("change-1", ["b.py", "a.py"]),
    )
    tracker.observe(
        _call("apply_workspace_edits", "call-2"),
        _result("change-2", ["a.py", "中文.md"], call_id="call-2"),
    )

    assert tracker.paths() == ("a.py", "b.py", "中文.md")


def test_tracker_removes_undone_change() -> None:
    tracker = AppliedChangeTracker()
    tracker.observe(
        _call("apply_workspace_edits"),
        _result("change-1", ["app/main.py"]),
    )

    tracker.observe(
        _call("undo_workspace_change"),
        _result("change-1", ["app/main.py"]),
    )

    assert tracker.paths() == ()


def test_tracker_ignores_errors_unrelated_tools_and_invalid_payloads() -> None:
    tracker = AppliedChangeTracker()

    tracker.observe(
        _call("apply_workspace_edits"),
        ToolResult(call_id="call-1", output="not-json"),
    )
    tracker.observe(
        _call("read_file"),
        _result("change-1", ["ignored.py"]),
    )
    tracker.observe(
        _call("apply_workspace_edits"),
        ToolResult(
            call_id="call-1",
            output='{"data":{"change_id":"change-1","paths":"bad"}}',
        ),
    )
    tracker.observe(
        _call("apply_workspace_edits"),
        ToolResult(
            call_id="call-1",
            output='{"data":{"change_id":"change-1","paths":["bad.py"]}}',
            is_error=True,
        ),
    )

    assert tracker.paths() == ()
