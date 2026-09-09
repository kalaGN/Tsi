import asyncio

from textual.widgets import RichLog, TextArea

from app.runtime.chat import (
    ChatErrorCode,
    ChatResult,
    ChatRuntimeError,
    ChatRuntimeInfo,
)
from app.tui.application import ChatTuiApp
from app.tui.bootstrap import injected_tui_dependencies
from app.tui.state import RunStatus


def _transcript(app):
    return chr(10).join(
        line.text for line in app.query_one(RichLog).lines
    )


def test_request_coordinator_completes_and_cleans_worker_timer():
    async def scenario():
        async def runner(text, **callbacks):
            callbacks["on_text_delta"]("partial")
            return ChatResult("complete", "fake", "fake-model")

        app = ChatTuiApp(
            injected_tui_dependencies(
                chat_runner=runner,
                runtime_info=ChatRuntimeInfo("fake", "fake-model", True),
            )
        )
        async with app.run_test() as pilot:
            app.query_one("#prompt", TextArea).load_text("hello")
            await pilot.press("enter")
            await app.workers.wait_for_complete()

            assert "Assistant" in _transcript(app)
            assert "complete" in _transcript(app)
            assert app.run_status is RunStatus.READY
            assert not app._request.is_active
            assert app._request.activity_timer is None

    asyncio.run(scenario())


def test_request_coordinator_maps_runtime_error_and_cleans_state():
    async def scenario():
        async def runner(text, **callbacks):
            raise ChatRuntimeError(
                ChatErrorCode.TIMEOUT,
                "Upstream request timed out",
            )

        app = ChatTuiApp(
            injected_tui_dependencies(
                chat_runner=runner,
                runtime_info=ChatRuntimeInfo("fake", "fake-model", True),
            )
        )
        async with app.run_test() as pilot:
            app.query_one("#prompt", TextArea).load_text("hello")
            await pilot.press("enter")
            await app.workers.wait_for_complete()

            assert "Upstream request timed out" in _transcript(app)
            assert app.run_status is RunStatus.ERROR
            assert not app._request.is_active
            assert app._request.activity_timer is None

    asyncio.run(scenario())


def test_cancelled_request_rejects_late_result():
    async def scenario():
        started = asyncio.Event()

        async def runner(text, **callbacks):
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                callbacks["on_text_delta"]("stale delta")
                return ChatResult("stale answer", "fake", "fake-model")

        app = ChatTuiApp(
            injected_tui_dependencies(
                chat_runner=runner,
                runtime_info=ChatRuntimeInfo("fake", "fake-model", True),
            )
        )
        async with app.run_test() as pilot:
            app.query_one("#prompt", TextArea).load_text("hello")
            await pilot.press("enter")
            await started.wait()
            old_generation = app._request.generation

            await pilot.press("escape")
            await app.workers.wait_for_complete()

            transcript = _transcript(app)
            assert "stale answer" not in transcript
            assert "stale delta" not in transcript
            assert app._request.generation == old_generation + 1
            assert not app._request.is_active
            assert app._request.activity_timer is None

    asyncio.run(scenario())
