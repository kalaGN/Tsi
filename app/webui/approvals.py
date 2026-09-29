"""Web 写工具的一次性请求级审批协调。"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from app.runtime.task_preflight import TaskDecision
from tools.contracts import (
    AnyToolApprovalRequest,
    McpApprovalRequest,
    ScriptApprovalRequest,
    SkillInstallApprovalRequest,
    ToolApprovalRequest,
)


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

    def __init__(self, *, plan_timeout_seconds: float = 300.0) -> None:
        self._pending: _PendingApproval | None = None
        self._plan_timeout_seconds = plan_timeout_seconds

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    async def request(
        self,
        request_id: str,
        request: AnyToolApprovalRequest,
        emit: ApprovalEventHandler,
    ) -> bool:
        """注册一个已经过 Registry 校验的交互审批并等待一次性决定。"""

        if not isinstance(
            request,
            (
                ToolApprovalRequest,
                McpApprovalRequest,
                ScriptApprovalRequest,
                SkillInstallApprovalRequest,
            ),
        ):
            raise ValueError("unsupported web approval type")
        payload = {
            "tool": request.tool_name,
            "title": request.title,
        }
        if isinstance(request, McpApprovalRequest):
            payload.update(
                paths=[f"Server：{request.server_name} · Tool：{request.remote_tool_name}"],
                diff=f"{request.warning_text}\n\n参数：\n{request.arguments_text}",
                approve_label="执行",
            )
        elif isinstance(request, SkillInstallApprovalRequest):
            payload.update(
                paths=[
                    f"来源：{request.source_display}",
                    f"目标：{request.target_path}",
                    f"访问网络：{'是' if request.network_access else '否'}",
                ],
                diff=request.warning_text,
                approve_label="安装",
            )
        elif isinstance(request, ScriptApprovalRequest):
            payload.update(
                paths=[
                    f"Skill：{request.skill_name}",
                    f"脚本：{request.script_path}",
                ],
                diff=f"{request.warning_text}\n\n命令：\n{request.command_text}",
                approve_label="执行",
            )
        else:
            payload.update(paths=list(request.paths), diff=request.diff_text)
        return await self._await_decision(request_id, payload, emit)

    async def request_plan(
        self, request_id: str, decision: TaskDecision, emit: ApprovalEventHandler,
    ) -> bool:
        """复用一次性审批通道，但计划确认不授予任何工具权限。"""

        if decision.kind != "planned":
            raise ValueError("only planned decisions require confirmation")
        lines = [decision.reason, "", "执行步骤："]
        lines.extend(
            f"{index}. {step.action}\n   产出：{step.deliverable}"
            for index, step in enumerate(decision.steps, 1)
        )
        try:
            async with asyncio.timeout(self._plan_timeout_seconds):
                return await self._await_decision(request_id, {
                    "tool": "task_plan",
                    "title": "确认任务计划",
                    "paths": [],
                    "diff": "\n".join(lines),
                    "approve_label": "执行",
                }, emit)
        except TimeoutError:
            # 超时等同拒绝；旧审批 ID 不可再解锁已经结束的请求。
            return False

    async def _await_decision(
        self, request_id: str, payload: dict[str, object], emit: ApprovalEventHandler,
    ) -> bool:
        """计划与工具共用同一请求级决策槽，拒绝过期或重复确认。"""

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
            emit({"approval_id": pending.approval_id, **payload})
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
