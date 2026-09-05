"""跟踪一次模型请求中已经落盘的工作区变更。"""

import json

from tools import ToolCall, ToolResult


class AppliedChangeTracker:
    """记录成功应用且尚未撤销的变更批次。"""

    _TRACKED_TOOLS = {"apply_workspace_edits", "undo_workspace_change"}

    def __init__(self) -> None:
        self._changes: dict[str, tuple[str, ...]] = {}

    def observe(self, call: ToolCall, result: ToolResult) -> None:
        """从成功的工作区工具结果中更新已应用变更。"""

        if result.is_error or call.name not in self._TRACKED_TOOLS:
            return
        parsed = self._parse_result(result.output)
        if parsed is None:
            return
        change_id, paths = parsed
        if call.name == "apply_workspace_edits":
            self._changes[change_id] = paths
        else:
            self._changes.pop(change_id, None)

    def paths(self) -> tuple[str, ...]:
        """返回仍保留在磁盘上的去重、排序后相对路径。"""

        return tuple(
            sorted({path for paths in self._changes.values() for path in paths})
        )

    @staticmethod
    def _parse_result(output: str) -> tuple[str, tuple[str, ...]] | None:
        """解析工具结果；格式不完整时忽略，避免影响主请求。"""

        try:
            payload = json.loads(output)
            data = payload["data"]
            change_id = data["change_id"]
            raw_paths = data["paths"]
        except (KeyError, TypeError, json.JSONDecodeError):
            return None
        if not isinstance(change_id, str) or not isinstance(raw_paths, list):
            return None
        if not all(isinstance(path, str) for path in raw_paths):
            return None
        return change_id, tuple(raw_paths)
