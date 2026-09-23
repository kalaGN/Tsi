"""长任务步骤复用现有事件流，结果必须通过验收。"""

import asyncio

from app.runtime.task_runner import run_task_step
from app.runtime.task_runs import TaskRunStore
from tools.workspace import WorkspacePolicy


def test_task_completion_requires_deterministic_evidence(tmp_path):
    store = TaskRunStore(tmp_path / "runs")
    task = store.create("session-1", "project-1", "创建 answer.txt", [
        {"kind": "file_exists", "target": "answer.txt", "expected_sha256": None},
    ])

    async def stream(_prompt):
        yield {"type": "request_started"}
        yield {"type": "completed", "output_text": "已完成"}

    async def collect(record):
        return [item async for item in run_task_step(record, store, stream, WorkspacePolicy(tmp_path))]

    first = asyncio.run(collect(task))
    assert first[-2]["task"]["state"] == "needs_review"
    assert first[-3]["result"]["status"] == "unknown"
    (tmp_path / "answer.txt").write_text("ok", encoding="utf-8")
    second = asyncio.run(collect(store.load(task.id)))
    assert second[-2]["task"]["state"] == "completed"


def test_task_approval_and_interrupted_step_never_auto_replay(tmp_path):
    store = TaskRunStore(tmp_path / "runs")
    task = store.create("session-1", "project-1", "修改文件", [])

    async def interrupted(_prompt):
        yield {"type": "tool_approval_required"}
        raise RuntimeError("stream broke")

    async def scenario():
        events = []
        try:
            async for item in run_task_step(task, store, interrupted, WorkspacePolicy(tmp_path)):
                events.append(item)
        except RuntimeError:
            pass
        return events

    events = asyncio.run(scenario())
    assert events[-2]["task"]["state"] == "awaiting_approval"
    assert events[-1]["type"] == "tool_approval_required"
    assert store.load(task.id).state == "needs_review"
    assert store.recover_interrupted() == ()


def test_task_timeout_is_visible_and_requires_review(tmp_path, monkeypatch):
    store = TaskRunStore(tmp_path / "runs")
    task = store.create("session-1", "project-1", "长任务", [])
    real_timeout = asyncio.timeout
    monkeypatch.setattr("app.runtime.task_runner.asyncio.timeout", lambda _: real_timeout(0.001))

    async def slow(_prompt):
        await asyncio.sleep(0.05)
        yield {"type": "completed", "output_text": "不应完成"}

    async def collect():
        return [item async for item in run_task_step(task, store, slow, WorkspacePolicy(tmp_path))]

    events = asyncio.run(collect())
    assert events[-1]["type"] == "failed"
    assert events[-2]["task"]["state"] == "needs_review"
    assert store.load(task.id).state == "needs_review"
