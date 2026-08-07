import asyncio
from pathlib import Path

from textual.widgets import RichLog, Static, TextArea

from app.runtime.chat import (
    ChatErrorCode,
    ChatResult,
    ChatRuntimeError,
    ChatRuntimeInfo,
)
from app.tui import __main__ as tui_main
from app.tui import application as tui_application
from app.tui.application import ChatTuiApp
from app.tui.state import RunStatus


ALIYUN_INFO = ChatRuntimeInfo("aliyun", "qwen3-max", True)
DEEPSEEK_INFO = ChatRuntimeInfo("deepseek", "deepseek-v4-flash", True)
MISSING_KEY_INFO = ChatRuntimeInfo("deepseek", "deepseek-v4-flash", False)


def transcript_text(app: ChatTuiApp) -> str:
    return "\n".join(
        line.text for line in app.query_one("#transcript", RichLog).lines
    )


def test_tui_initial_state_shows_provider_model_and_key_status():
    async def scenario():
        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test():
            status_bar = app.query_one("#status-bar", Static)

            assert app.run_status is RunStatus.READY
            assert "Aliyun" in str(status_bar.content)
            assert "qwen3-max" in str(status_bar.content)
            assert "Key: configured" in str(status_bar.content)
            assert "Ready" in str(status_bar.content)

    asyncio.run(scenario())


def test_tui_initial_state_supports_deepseek_status():
    async def scenario():
        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test():
            status = str(app.query_one("#status-bar", Static).content)

            assert "DeepSeek" in status
            assert "deepseek-v4-flash" in status
            assert "Key: configured" in status

    asyncio.run(scenario())


def test_tui_missing_key_starts_in_safe_error_state():
    async def scenario():
        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=MISSING_KEY_INFO)
        async with app.run_test():
            status_bar = app.query_one("#status-bar", Static)

            assert app.run_status is RunStatus.ERROR
            assert "Key: missing" in str(status_bar.content)
            assert "Upstream API key is not configured" in transcript_text(app)

    asyncio.run(scenario())


def test_tui_status_never_displays_api_key():
    async def scenario():
        secret_key = "test-secret-key-must-not-appear"

        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test():
            status_content = str(app.query_one("#status-bar", Static).content)

            assert "Key: configured" in status_content
            assert secret_key not in status_content
            assert secret_key not in transcript_text(app)

    asyncio.run(scenario())


def test_tui_invalid_provider_configuration_starts_in_safe_error_state(monkeypatch):
    async def scenario():
        def fail_to_get_info():
            raise ChatRuntimeError(
                ChatErrorCode.CONFIGURATION,
                "Unsupported LLM provider configuration",
            )

        monkeypatch.setattr(
            tui_application,
            "get_chat_runtime_info",
            fail_to_get_info,
        )
        app = ChatTuiApp()

        async with app.run_test():
            assert app.run_status is RunStatus.ERROR
            assert "Unsupported LLM provider configuration" in transcript_text(app)

    asyncio.run(scenario())


def test_tui_entrypoint_loads_project_env_without_overriding_shell(monkeypatch):
    observed = {}

    def fake_load_dotenv(path, override):
        observed["dotenv"] = (path, override)

    class FakeApp:
        def run(self):
            observed["run"] = True

    def fake_create_app():
        observed["kitty_keyboard_disabled"] = (
            tui_main.os.environ.get("TEXTUAL_DISABLE_KITTY_KEY")
        )
        return FakeApp()

    monkeypatch.delenv("TEXTUAL_DISABLE_KITTY_KEY", raising=False)
    monkeypatch.setattr(tui_main, "load_dotenv", fake_load_dotenv)
    monkeypatch.setattr(
        tui_main,
        "configure_model_logging",
        lambda: observed.setdefault("logging", "configured"),
    )
    monkeypatch.setattr(tui_main, "_create_app", fake_create_app)

    tui_main.main()

    expected_root = Path(tui_main.__file__).resolve().parents[2]
    assert observed == {
        "dotenv": (expected_root / ".env", False),
        "logging": "configured",
        "kitty_keyboard_disabled": "1",
        "run": True,
    }


def test_enter_submits_input():
    async def scenario():
        received_inputs = []
        clock_values = iter([10.0, 11.234])

        async def fake_runner(input_text: str) -> ChatResult:
            received_inputs.append(input_text)
            return ChatResult("answer", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=ALIYUN_INFO,
            clock=lambda: next(clock_values),
        )
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.focus()
            await pilot.press("h", "e", "l", "l", "o")

            await pilot.press("enter")
            await app.workers.wait_for_complete()

            assert received_inputs == ["hello"]
            assert prompt.text == ""
            assert app.run_status is RunStatus.READY
            transcript = transcript_text(app)
            assert "You\nhello" in transcript
            assert "Assistant\nanswer" in transcript
            assert "System\n耗时：1.23 秒" in transcript

    asyncio.run(scenario())


def test_runtime_error_displays_request_duration():
    async def scenario():
        clock_values = iter([20.0, 22.5])

        async def fake_runner(input_text: str) -> ChatResult:
            raise ChatRuntimeError(
                ChatErrorCode.TIMEOUT,
                "Upstream request timed out",
            )

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=ALIYUN_INFO,
            clock=lambda: next(clock_values),
        )
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")

            await pilot.press("enter")
            await app.workers.wait_for_complete()

            transcript = transcript_text(app)
            assert "Error\nUpstream request timed out" in transcript
            assert "System\n耗时：2.50 秒" in transcript

    asyncio.run(scenario())


def test_text_area_accepts_chinese_input_directly():
    async def scenario():
        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner should not be called while editing")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.focus()

            await pilot.press("中", "文", "输", "入")

            assert prompt.text == "中文输入"

    asyncio.run(scenario())


def test_blank_input_is_rejected_without_calling_runner():
    async def scenario():
        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner must not receive blank input")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("  \n")

            await pilot.press("enter")

            assert app.run_status is RunStatus.READY
            assert "Input must not be blank" in transcript_text(app)

    asyncio.run(scenario())


def test_help_and_chat_are_sent_as_ordinary_model_input():
    async def scenario():
        received_inputs = []

        async def fake_runner(input_text: str) -> ChatResult:
            received_inputs.append(input_text)
            return ChatResult("ok", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            for text in ("/help", "/chat"):
                prompt.load_text(text)
                await pilot.press("enter")
                await app.workers.wait_for_complete()

            assert received_inputs == ["/help", "/chat"]

    asyncio.run(scenario())


def test_clear_command_clears_transcript_without_calling_runner():
    async def scenario():
        received_inputs = []

        async def fake_runner(input_text: str) -> ChatResult:
            received_inputs.append(input_text)
            return ChatResult("answer", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert transcript_text(app)

            prompt.load_text(" /clear \n")
            await pilot.press("enter")

            assert transcript_text(app) == ""
            assert received_inputs == ["hello"]
            assert app.run_status is RunStatus.READY

    asyncio.run(scenario())


def test_thinking_state_blocks_duplicate_submission():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        received_inputs = []

        async def fake_runner(input_text: str) -> ChatResult:
            received_inputs.append(input_text)
            started.set()
            await release.wait()
            return ChatResult("done", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("first")
            await pilot.press("enter")
            await asyncio.wait_for(started.wait(), timeout=1)

            assert app.run_status is RunStatus.THINKING

            prompt.load_text("second")
            await pilot.press("enter")
            assert received_inputs == ["first"]
            assert prompt.text == "second"

            release.set()
            await app.workers.wait_for_complete()

    asyncio.run(scenario())


def test_runtime_error_is_recoverable_on_next_submission():
    async def scenario():
        attempts = 0

        async def fake_runner(input_text: str) -> ChatResult:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ChatRuntimeError(
                    ChatErrorCode.TIMEOUT,
                    "Upstream request timed out",
                )
            return ChatResult("recovered", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("first")
            await pilot.press("enter")
            await app.workers.wait_for_complete()

            assert app.run_status is RunStatus.ERROR
            assert "Upstream request timed out" in transcript_text(app)

            prompt.load_text("second")
            await pilot.press("enter")
            await app.workers.wait_for_complete()

            assert app.run_status is RunStatus.READY
            assert "recovered" in transcript_text(app)

    asyncio.run(scenario())


def test_unexpected_error_is_hidden_and_app_remains_available():
    async def scenario():
        secret_detail = "internal-secret-detail"

        async def fake_runner(input_text: str) -> ChatResult:
            raise RuntimeError(secret_detail)

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")
            await pilot.press("enter")
            await app.workers.wait_for_complete()

            transcript = transcript_text(app)
            assert app.run_status is RunStatus.ERROR
            assert "Unexpected internal error" in transcript
            assert secret_detail not in transcript

    asyncio.run(scenario())


def test_double_escape_cancels_active_request_then_exits():
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()
        exit_called = False
        clock_values = iter([10.0, 10.2, 10.3])

        async def fake_runner(input_text: str) -> ChatResult:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                return ChatResult("late result", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=ALIYUN_INFO,
            clock=lambda: next(clock_values),
        )
        original_exit = app.exit

        def record_exit(*args, **kwargs):
            nonlocal exit_called
            exit_called = True
            return original_exit(*args, **kwargs)

        app.exit = record_exit
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            transcript_widget = app.query_one("#transcript", RichLog)
            prompt.load_text("hello")
            await pilot.press("enter")
            await asyncio.wait_for(started.wait(), timeout=1)

            await pilot.press("escape")
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            assert not exit_called
            assert "System\n再次按 Esc 退出" in transcript_text(app)
            assert "耗时：" not in transcript_text(app)

            await pilot.press("escape")

        assert exit_called
        transcript = "\n".join(line.text for line in transcript_widget.lines)
        assert "耗时：" not in transcript

    asyncio.run(scenario())


def test_quit_command_exits_without_calling_runner():
    async def scenario():
        exit_called = False

        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner must not receive /quit")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        original_exit = app.exit

        def record_exit(*args, **kwargs):
            nonlocal exit_called
            exit_called = True
            return original_exit(*args, **kwargs)

        app.exit = record_exit
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("/quit")
            await pilot.press("enter")

        assert exit_called

    asyncio.run(scenario())


def test_double_escape_exits_when_no_request_is_running():
    async def scenario():
        exit_called = False

        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner should not be called")

        clock_values = iter([10.0, 10.5])
        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=ALIYUN_INFO,
            clock=lambda: next(clock_values),
        )
        original_exit = app.exit

        def record_exit(*args, **kwargs):
            nonlocal exit_called
            exit_called = True
            return original_exit(*args, **kwargs)

        app.exit = record_exit
        async with app.run_test() as pilot:
            await pilot.press("escape")
            assert not exit_called
            assert "再次按 Esc 退出" in transcript_text(app)

            await pilot.press("escape")

        assert exit_called

    asyncio.run(scenario())


def test_escape_confirmation_expires_after_timeout():
    async def scenario():
        exit_called = False
        clock_values = iter([10.0, 12.0, 12.5])

        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner should not be called")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=ALIYUN_INFO,
            clock=lambda: next(clock_values),
        )
        original_exit = app.exit

        def record_exit(*args, **kwargs):
            nonlocal exit_called
            exit_called = True
            return original_exit(*args, **kwargs)

        app.exit = record_exit
        async with app.run_test() as pilot:
            await pilot.press("escape")
            await pilot.press("escape")
            assert not exit_called

            await pilot.press("escape")

        assert exit_called

    asyncio.run(scenario())


def test_quit_command_cancels_active_request_before_exit():
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()
        exit_called = False

        async def fake_runner(input_text: str) -> ChatResult:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        original_exit = app.exit

        def record_exit(*args, **kwargs):
            nonlocal exit_called
            exit_called = True
            return original_exit(*args, **kwargs)

        app.exit = record_exit
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")
            await pilot.press("enter")
            await asyncio.wait_for(started.wait(), timeout=1)

            prompt.load_text("/quit")
            await pilot.press("enter")

        assert exit_called
        assert cancelled.is_set()

    asyncio.run(scenario())
