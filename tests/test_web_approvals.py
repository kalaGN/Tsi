import asyncio

import pytest

from app.webui.approvals import (
    WebApprovalConflict,
    WebApprovalCoordinator,
    WebApprovalNotFound,
)
from tools.contracts import ToolApprovalRequest


def approval_request() -> ToolApprovalRequest:
    return ToolApprovalRequest(
        call_id="call-1",
        tool_name="apply_workspace_edits",
        title="应用工作区修改",
        paths=("app/example.py",),
        diff_text="--- a/app/example.py\n+++ b/app/example.py\n",
        fingerprint="a" * 64,
    )


def test_web_approval_resolves_once_without_exposing_fingerprint():
    async def scenario():
        coordinator = WebApprovalCoordinator()
        emitted = []
        task = asyncio.create_task(
            coordinator.request("request-1", approval_request(), emitted.append)
        )
        await asyncio.sleep(0)

        payload = emitted[0]
        result = coordinator.resolve(
            payload["approval_id"],
            "request-1",
            True,
        )

        assert result == {"accepted": True}
        assert await task is True
        assert payload == {
            "approval_id": payload["approval_id"],
            "tool": "apply_workspace_edits",
            "title": "应用工作区修改",
            "paths": ["app/example.py"],
            "diff": "--- a/app/example.py\n+++ b/app/example.py\n",
        }
        assert "fingerprint" not in payload
        with pytest.raises(WebApprovalNotFound):
            coordinator.resolve(payload["approval_id"], "request-1", True)

    asyncio.run(scenario())


def test_web_approval_rejects_cross_request_without_consuming_decision():
    async def scenario():
        coordinator = WebApprovalCoordinator()
        emitted = []
        task = asyncio.create_task(
            coordinator.request("request-1", approval_request(), emitted.append)
        )
        await asyncio.sleep(0)
        approval_id = emitted[0]["approval_id"]

        with pytest.raises(WebApprovalConflict):
            coordinator.resolve(approval_id, "request-2", True)

        coordinator.resolve(approval_id, "request-1", False)
        assert await task is False

    asyncio.run(scenario())


def test_web_approval_invalidation_cancels_waiter_and_expires_id():
    async def scenario():
        coordinator = WebApprovalCoordinator()
        emitted = []
        task = asyncio.create_task(
            coordinator.request("request-1", approval_request(), emitted.append)
        )
        await asyncio.sleep(0)
        approval_id = emitted[0]["approval_id"]

        coordinator.invalidate("request-1")

        with pytest.raises(asyncio.CancelledError):
            await task
        assert coordinator.has_pending is False
        with pytest.raises(WebApprovalNotFound):
            coordinator.resolve(approval_id, "request-1", True)

    asyncio.run(scenario())
