"""记录一次请求中已应用且未撤销的工作区变更。"""

import json

from tools import ToolCall, ToolResult


class AppliedChangeTracker:
    """只从工具成功结果提取相对路径，不保存调用参数或正文。"""

    _TRACKED_TOOLS = {
        "apply_workspace_edits", "delete_workspace_file", "undo_workspace_change",
    }

    def __init__(self) -> None:
        self._changes: dict[str, tuple[str, ...]] = {}

    def observe(self, call: ToolCall, result: ToolResult) -> None:
        """在成功的写入/撤销调用之后更新已落盘文件集合。"""

        if result.is_error or call.name not in self._TRACKED_TOOLS:
            return
        parsed = self._parse_result(call.name, result.output)
        if parsed is None:
            return
        change_id, paths = parsed
        if call.name == "undo_workspace_change":
            self._changes.pop(change_id, None)
        else:
            self._changes[change_id] = paths

    def paths(self) -> tuple[str, ...]:
        """返回有界的、排序去重后的相对路径。"""

        return tuple(sorted({path for paths in self._changes.values() for path in paths}))[:50]

    @staticmethod
    def _parse_result(tool_name: str, output: str) -> tuple[str, tuple[str, ...]] | None:
        """工具结果格式异常时不影响主请求。"""

        try:
            payload = json.loads(output)
            data = payload["data"]
            change_id = data["change_id"]
        except (KeyError, TypeError, json.JSONDecodeError):
            return None
        if not isinstance(change_id, str):
            return None
        if tool_name == "delete_workspace_file":
            path = data.get("path")
            return (change_id, (path,)) if isinstance(path, str) else None
        raw_paths = data.get("paths")
        if not isinstance(raw_paths, list) or not all(isinstance(path, str) for path in raw_paths):
            return None
        return change_id, tuple(raw_paths)
