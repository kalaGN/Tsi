"""TUI 任务命令经真实请求协调器更新持久状态。"""

import asyncio
from dataclasses import replace

from textual.widgets import RichLog, TextArea

from app.runtime.chat import ChatResult, ChatRuntimeInfo
from app.runtime.task_runs import TaskRunStore
from app.tui.application import ChatTuiApp
from app.tui.bootstrap import injected_tui_dependencies
from tools.workspace import WorkspacePolicy


def test_tui_task_creation_verification_and_status(tmp_path):
    (tmp_path / "answer.txt").write_text("ok", encoding="utf-8")
    store = TaskRunStore(tmp_path / "task-runs")
    calls = []

    async def runner(text, **_kwargs):
        calls.append(text)
        return ChatResult("已检查", "fake", "fake")

    dependencies = injected_tui_dependencies(
        chat_runner=runner, runtime_info=ChatRuntimeInfo("deepseek", "test", True),
    )
    app = ChatTuiApp(replace(dependencies, task_policy=WorkspacePolicy(tmp_path), task_store=store))

    async def scenario():
        async with app.run_test() as pilot:
            app.query_one("#prompt", TextArea).load_text("/task new 检查 answer.txt | file_exists:answer.txt")
            await pilot.press("enter")
            await pilot.pause()
            assert calls == ["检查 answer.txt"]
            assert store.list()[0].state == "completed"
            app.query_one("#prompt", TextArea).load_text("/task")
            await pilot.press("enter")
            assert "completed" in "\n".join(line.text for line in app.query_one("#transcript", RichLog).lines)

    asyncio.run(scenario())


def test_tui_unverified_task_requires_explicit_resume(tmp_path):
    store = TaskRunStore(tmp_path / "task-runs")
    calls = []

    async def runner(text, **_kwargs):
        calls.append(text)
        return ChatResult("我完成了", "fake", "fake")

    dependencies = injected_tui_dependencies(
        chat_runner=runner, runtime_info=ChatRuntimeInfo("deepseek", "test", True),
    )
    app = ChatTuiApp(replace(dependencies, task_policy=WorkspacePolicy(tmp_path), task_store=store))

    async def scenario():
        async with app.run_test() as pilot:
            app.query_one("#prompt", TextArea).load_text("/task new 创建 answer.txt | file_exists:answer.txt")
            await pilot.press("enter")
            await pilot.pause()
            task = store.list()[0]
            assert task.state == "needs_review"
            assert len(calls) == 1
            (tmp_path / "answer.txt").write_text("ok", encoding="utf-8")
            app.query_one("#prompt", TextArea).load_text(f"/task resume {task.id}")
            await pilot.press("enter")
            await pilot.pause()
            assert store.load(task.id).state == "completed"
            assert len(calls) == 2

    asyncio.run(scenario())
