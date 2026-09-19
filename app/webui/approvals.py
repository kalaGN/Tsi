"""Web 写工具的一次性请求级审批协调。"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from tools.contracts import AnyToolApprovalRequest, ToolApprovalRequest


class WebApprovalNotFound(Exception):
    """当前没有与给定审批 ID 匹配的待处理操作。"""


class WebApprovalConflict(Exception):
    """审批存在，但提交的请求 ID 与其所属请求不一致。"""


@dataclass(frozen=True)
class _PendingApproval:
    approval_id: str
    request_id: str
    future: asyncio.Future[bool]


ApprovalEventHandler = Callable[[dict[str, object]], None]


class WebApprovalCoordinator:
    """把同步 HTTP 决策桥接到工具循环正在等待的异步审批。"""

    def __init__(self) -> None:
        self._pending: _PendingApproval | None = None

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    async def request(
        self,
        request_id: str,
        request: AnyToolApprovalRequest,
        emit: ApprovalEventHandler,
    ) -> bool:
        """注册一个已经过 Registry 校验的文件审批并等待一次性决定。"""

        if not isinstance(request, ToolApprovalRequest):
            raise ValueError("unsupported web approval type")
        if self._pending is not None:
            raise WebApprovalConflict("another approval is pending")

        future = asyncio.get_running_loop().create_future()
        pending = _PendingApproval(
            approval_id=secrets.token_urlsafe(24),
            request_id=request_id,
            future=future,
        )
        self._pending = pending
        try:
            emit(
                {
                    "approval_id": pending.approval_id,
                    "tool": request.tool_name,
                    "title": request.title,
                    "paths": list(request.paths),
                    "diff": request.diff_text,
                }
            )
            return await future
        finally:
            if self._pending is pending:
                self._pending = None

    def resolve(
        self,
        approval_id: str,
        request_id: str,
        approved: bool,
    ) -> dict[str, bool]:
        """只完成当前请求尚未决策的审批，拒绝过期或重复提交。"""

        pending = self._pending
        if pending is None or pending.approval_id != approval_id:
            raise WebApprovalNotFound("approval not found")
        if pending.request_id != request_id:
            raise WebApprovalConflict("approval request does not match")
        if pending.future.done():
            raise WebApprovalNotFound("approval already resolved")
        pending.future.set_result(approved)
        return {"accepted": True}

    def invalidate(self, request_id: str) -> None:
        """取消指定请求尚未完成的审批，避免断流后仍可执行。"""

        pending = self._pending
        if pending is None or pending.request_id != request_id:
            return
        if not pending.future.done():
            pending.future.cancel()
        if self._pending is pending:
            self._pending = None
