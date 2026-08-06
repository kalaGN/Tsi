import asyncio
from pathlib import Path

from textual.widgets import RichLog, Static, TextArea

from app.runtime.chat import ChatErrorCode, ChatResult, ChatRuntimeError
from app.tui import __main__ as tui_main
from app.tui.application import ChatTuiApp, format_response_body
from app.tui.state import RunStatus


def transcript_text(app: ChatTuiApp) -> str:
    return "\n".join(
        line.text for line in app.query_one("#transcript", RichLog).lines
    )


def test_format_response_body_prefers_top_level_output_text():
    body = {"output_text": "top level", "output": [{"text": "nested"}]}

    assert format_response_body(body) == "top level"


def test_format_response_body_prefers_direct_output_text_fragments():
    body = {
        "output": [
            {"text": "first"},
            {"content": [{"text": "second"}, {"type": "metadata"}]},
        ]
    }

    assert format_response_body(body) == "first"


def test_format_response_body_collects_nested_content_text_fragments():
    body = {
        "output": [
            {"content": [{"text": "first"}, {"type": "metadata"}]},
            {"content": [{"text": "second"}]},
        ]
    }

    assert format_response_body(body) == "first\nsecond"


def test_format_response_body_falls_back_to_readable_json():
    body = {"id": "response-1", "usage": {"total_tokens": 3}}

    assert format_response_body(body) == (
        '{\n  "id": "response-1",\n  "usage": {\n    "total_tokens": 3\n  }\n}'
    )


def test_tui_initial_state_shows_provider_model_and_key_status():
    async def scenario():
        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, api_key_configured=True)
        async with app.run_test():
            status_bar = app.query_one("#status-bar", Static)

            assert app.run_status is RunStatus.READY
            assert "Aliyun" in str(status_bar.content)
            assert "qwen3-max" in str(status_bar.content)
            assert "Key: configured" in str(status_bar.content)
            assert "Ready" in str(status_bar.content)

    asyncio.run(scenario())


def test_tui_missing_key_starts_in_safe_error_state():
    async def scenario():
        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, api_key_configured=False)
        async with app.run_test():
            status_bar = app.query_one("#status-bar", Static)

            assert app.run_status is RunStatus.ERROR
            assert "Key: missing" in str(status_bar.content)
            assert "Upstream API key is not configured" in transcript_text(app)

    asyncio.run(scenario())


def test_tui_status_never_displays_api_key(monkeypatch):
    async def scenario():
        secret_key = "test-secret-key-must-not-appear"
        monkeypatch.setenv("DASHSCOPE_API_KEY", secret_key)

        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner)
        async with app.run_test():
            status_content = str(app.query_one("#status-bar", Static).content)

            assert "Key: configured" in status_content
            assert secret_key not in status_content
            assert secret_key not in transcript_text(app)

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
    monkeypatch.setattr(tui_main, "_create_app", fake_create_app)

    tui_main.main()

    expected_root = Path(tui_main.__file__).resolve().parents[2]
    assert observed == {
        "dotenv": (expected_root / ".env", False),
        "kitty_keyboard_disabled": "1",
        "run": True,
    }


def test_enter_adds_newline_and_ctrl_s_submits_multiline_input():
    async def scenario():
        received_inputs = []

        async def fake_runner(input_text: str) -> ChatResult:
            received_inputs.append(input_text)
            return ChatResult(200, {"output_text": "answer"})

        app = ChatTuiApp(chat_runner=fake_runner, api_key_configured=True)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.focus()
            await pilot.press("h", "i", "enter", "t", "h", "e", "r", "e")

            assert prompt.text == "hi\nthere"

            await pilot.press("ctrl+s")
            await app.workers.wait_for_complete()

            assert received_inputs == ["hi\nthere"]
            assert prompt.text == ""
            assert app.run_status is RunStatus.READY
            transcript = transcript_text(app)
            assert "You\nhi\nthere" in transcript
            assert "Assistant\nanswer" in transcript

    asyncio.run(scenario())


def test_text_area_accepts_chinese_input_directly():
    async def scenario():
        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner should not be called while editing")

        app = ChatTuiApp(chat_runner=fake_runner, api_key_configured=True)
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

        app = ChatTuiApp(chat_runner=fake_runner, api_key_configured=True)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("  \n")

            await pilot.press("ctrl+s")

            assert app.run_status is RunStatus.READY
            assert "Input must not be blank" in transcript_text(app)

    asyncio.run(scenario())


def test_help_and_chat_are_sent_as_ordinary_model_input():
    async def scenario():
        received_inputs = []

        async def fake_runner(input_text: str) -> ChatResult:
            received_inputs.append(input_text)
            return ChatResult(200, {"output_text": "ok"})

        app = ChatTuiApp(chat_runner=fake_runner, api_key_configured=True)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            for text in ("/help", "/chat"):
                prompt.load_text(text)
                await pilot.press("ctrl+s")
                await app.workers.wait_for_complete()

            assert received_inputs == ["/help", "/chat"]

    asyncio.run(scenario())


def test_clear_command_clears_transcript_without_calling_runner():
    async def scenario():
        received_inputs = []

        async def fake_runner(input_text: str) -> ChatResult:
            received_inputs.append(input_text)
            return ChatResult(200, {"output_text": "answer"})

        app = ChatTuiApp(chat_runner=fake_runner, api_key_configured=True)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")
            await pilot.press("ctrl+s")
            await app.workers.wait_for_complete()
            assert transcript_text(app)

            prompt.load_text(" /clear \n")
            await pilot.press("ctrl+s")

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
            return ChatResult(200, {"output_text": "done"})

        app = ChatTuiApp(chat_runner=fake_runner, api_key_configured=True)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("first")
            await pilot.press("ctrl+s")
            await asyncio.wait_for(started.wait(), timeout=1)

            assert app.run_status is RunStatus.THINKING

            prompt.load_text("second")
            await pilot.press("ctrl+s")
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
            return ChatResult(200, {"output_text": "recovered"})

        app = ChatTuiApp(chat_runner=fake_runner, api_key_configured=True)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("first")
            await pilot.press("ctrl+s")
            await app.workers.wait_for_complete()

            assert app.run_status is RunStatus.ERROR
            assert "Upstream request timed out" in transcript_text(app)

            prompt.load_text("second")
            await pilot.press("ctrl+s")
            await app.workers.wait_for_complete()

            assert app.run_status is RunStatus.READY
            assert "recovered" in transcript_text(app)

    asyncio.run(scenario())


def test_unexpected_error_is_hidden_and_app_remains_available():
    async def scenario():
        secret_detail = "internal-secret-detail"

        async def fake_runner(input_text: str) -> ChatResult:
            raise RuntimeError(secret_detail)

        app = ChatTuiApp(chat_runner=fake_runner, api_key_configured=True)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")
            await pilot.press("ctrl+s")
            await app.workers.wait_for_complete()

            transcript = transcript_text(app)
            assert app.run_status is RunStatus.ERROR
            assert "Unexpected internal error" in transcript
            assert secret_detail not in transcript

    asyncio.run(scenario())


def test_ctrl_c_cancels_active_request_without_writing_late_result():
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def fake_runner(input_text: str) -> ChatResult:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                return ChatResult(200, {"output_text": "late result"})

        app = ChatTuiApp(chat_runner=fake_runner, api_key_configured=True)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")
            await pilot.press("ctrl+s")
            await asyncio.wait_for(started.wait(), timeout=1)

            await pilot.press("ctrl+c")
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            await pilot.pause()

            transcript = transcript_text(app)
            assert app.run_status is RunStatus.READY
            assert "Request cancelled" in transcript
            assert "late result" not in transcript

    asyncio.run(scenario())


def test_quit_command_exits_without_calling_runner():
    async def scenario():
        exit_called = False

        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner must not receive /quit")

        app = ChatTuiApp(chat_runner=fake_runner, api_key_configured=True)
        original_exit = app.exit

        def record_exit(*args, **kwargs):
            nonlocal exit_called
            exit_called = True
            return original_exit(*args, **kwargs)

        app.exit = record_exit
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("/quit")
            await pilot.press("ctrl+s")

        assert exit_called

    asyncio.run(scenario())


def test_ctrl_c_exits_when_no_request_is_running():
    async def scenario():
        exit_called = False

        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner should not be called")

        app = ChatTuiApp(chat_runner=fake_runner, api_key_configured=True)
        original_exit = app.exit

        def record_exit(*args, **kwargs):
            nonlocal exit_called
            exit_called = True
            return original_exit(*args, **kwargs)

        app.exit = record_exit
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")

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

        app = ChatTuiApp(chat_runner=fake_runner, api_key_configured=True)
        original_exit = app.exit

        def record_exit(*args, **kwargs):
            nonlocal exit_called
            exit_called = True
            return original_exit(*args, **kwargs)

        app.exit = record_exit
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")
            await pilot.press("ctrl+s")
            await asyncio.wait_for(started.wait(), timeout=1)

            prompt.load_text("/quit")
            await pilot.press("ctrl+s")

        assert exit_called
        assert cancelled.is_set()

    asyncio.run(scenario())
