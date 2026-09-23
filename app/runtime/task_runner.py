"""把一次现有会话请求包成可验证的任务步骤，不重放不确定的副作用。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from app.runtime.task_runs import TaskRun, TaskRunStore
from app.runtime.task_verify import verify_task
from tools.workspace import WorkspacePolicy


EventStream = Callable[[str], AsyncIterator[dict[str, object]]]


async def run_task_step(
    task: TaskRun, store: TaskRunStore, stream: EventStream, policy: WorkspacePolicy,
) -> AsyncIterator[dict[str, object]]:
    """每次继续只增加一个完整对话轮次；断流后的结果一律人工检查。"""

    if task.state not in {"ready", "needs_review"}:
        raise ValueError("任务当前不可执行。")
    current = store.transition(task.id, task.revision, "running")
    settled = False
    yield {"type": "task_state", "task": current.public_payload()}
    prompt = task.goal if task.attempts == 0 else (
        f"继续完成此前任务：{task.goal}\n"
        "先检查现有会话和工作区状态；不要假设上一次未完成的写操作没有生效。"
    )
    try:
        async with asyncio.timeout(600):
            async for event in stream(prompt):
                event_type = event.get("type")
                if event_type == "tool_approval_required" and current.state == "running":
                    current = store.transition(current.id, current.revision, "awaiting_approval")
                    yield {"type": "task_state", "task": current.public_payload()}
                elif event_type == "tool_finished" and current.state == "awaiting_approval":
                    current = store.transition(current.id, current.revision, "running")
                    yield {"type": "task_state", "task": current.public_payload()}
                if event_type == "completed":
                    current = store.transition(current.id, current.revision, "verifying")
                    yield {"type": "task_state", "task": current.public_payload()}
                    verification = await verify_task(current.conditions, policy)
                    outcome = "completed" if verification.status == "passed" else "needs_review"
                    current = store.transition(
                        current.id, current.revision, outcome,
                        last_result=("验收通过。" if outcome == "completed" else "验收未全部通过，请检查证据。"),
                    )
                    settled = True
                    yield {"type": "task_verification", "result": verification.payload()}
                    yield {"type": "task_state", "task": current.public_payload()}
                    yield event
                    return
                if event_type in {"failed", "cancelled"}:
                    outcome = "cancelled" if event_type == "cancelled" else "needs_review"
                    current = store.transition(current.id, current.revision, outcome,
                                               last_result="任务已取消。" if outcome == "cancelled" else "请求失败，请先检查工作区。")
                    settled = True
                    yield {"type": "task_state", "task": current.public_payload()}
                    yield event
                    return
                yield event
    except TimeoutError:
        current = store.load(task.id)
        if current.state in {"running", "awaiting_approval", "verifying"}:
            current = store.transition(current.id, current.revision, "needs_review",
                                       last_result="本次执行超时，请先检查工作区。")
        settled = True
        yield {"type": "task_state", "task": current.public_payload()}
        yield {"type": "failed", "message": "任务执行超时，请检查工作区后决定是否继续。"}
    finally:
        if not settled:
            # 断流、超时或崩溃前可能已有副作用；只能等待人工核查。
            try:
                latest = store.load(current.id)
                if latest.state in {"running", "awaiting_approval", "verifying"}:
                    store.transition(latest.id, latest.revision, "needs_review",
                                     last_result="执行中断，请先检查工作区。")
            except Exception:
                # 保留原异常；下次启动仍会把非终态标为需要核查。
                pass
