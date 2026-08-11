import asyncio
from pathlib import Path

from textual.widgets import RichLog, Static, TextArea

from app.runtime.chat import (
    ChatErrorCode,
    ChatResult,
    ChatRuntimeError,
    ChatRuntimeInfo,
)
from app.runtime.session import ChatSession
from app.runtime.session_store import SessionStore, SessionStoreError
from app.services.llm.contracts import ChatMessage, ChatRole
from app.tui import __main__ as tui_main
from app.tui import application as tui_application
from app.tui.application import ChatTuiApp
from app.tui.state import RunStatus


ALIYUN_INFO = ChatRuntimeInfo("aliyun", "qwen3-max", True)
DEEPSEEK_INFO = ChatRuntimeInfo("deepseek", "deepseek-v4-flash", True)
MISSING_KEY_INFO = ChatRuntimeInfo("deepseek", "deepseek-v4-flash", False)


class ManualClock:
    """为实时活动栏和退出窗口测试提供可重复读取的单调时钟。"""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


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


def test_activity_bar_starts_empty_above_prompt():
    async def scenario():
        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner should not be called during startup")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=DEEPSEEK_INFO)
        async with app.run_test():
            activity = app.query_one("#activity-bar", Static)
            prompt = app.query_one("#prompt", TextArea)
            status = app.query_one("#status-bar", Static)

            assert str(activity.content) == ""
            assert activity.region.y < prompt.region.y < status.region.y
            assert app._activity_timer is None

    asyncio.run(scenario())


def test_activity_bar_updates_elapsed_time_and_clears_after_success():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        clock = ManualClock(10.0)

        async def fake_runner(input_text: str) -> ChatResult:
            started.set()
            await release.wait()
            return ChatResult("done", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=DEEPSEEK_INFO,
            clock=clock,
        )
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("hello")
            await pilot.press("enter")
            await asyncio.wait_for(started.wait(), timeout=1)

            activity = app.query_one("#activity-bar", Static)
            initial = str(activity.content)
            assert "思考中" in initial
            assert "0.0 秒" in initial
            assert "Esc 取消" in initial
            assert app._activity_timer is not None

            clock.advance(1.2)
            await pilot.pause(0.15)
            updated = str(activity.content)
            assert "1.2 秒" in updated
            assert updated != initial

            release.set()
            await app.workers.wait_for_complete()

            assert str(activity.content) == ""
            assert app._activity_timer is None
            assert "System\n耗时：1.20 秒" in transcript_text(app)

    asyncio.run(scenario())


def test_activity_bar_clears_after_known_and_unexpected_errors():
    async def scenario():
        async def known_error(input_text: str) -> ChatResult:
            raise ChatRuntimeError(
                ChatErrorCode.TIMEOUT,
                "Upstream request timed out",
            )

        async def unexpected_error(input_text: str) -> ChatResult:
            raise RuntimeError("internal detail")

        for runner in (known_error, unexpected_error):
            app = ChatTuiApp(chat_runner=runner, runtime_info=DEEPSEEK_INFO)
            async with app.run_test() as pilot:
                prompt = app.query_one("#prompt", TextArea)
                prompt.load_text("hello")
                await pilot.press("enter")
                await app.workers.wait_for_complete()

                assert str(app.query_one("#activity-bar", Static).content) == ""
                assert app._activity_timer is None

    asyncio.run(scenario())


def test_cancelled_worker_and_timer_cannot_clear_new_request_activity():
    async def scenario():
        first_started = asyncio.Event()
        first_cancelled = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()
        release_second = asyncio.Event()
        clock = ManualClock(10.0)
        call_count = 0

        async def fake_runner(input_text: str) -> ChatResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                first_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    first_cancelled.set()
                    await release_first.wait()
                    return ChatResult("late first", "fake", "fake-model")

            second_started.set()
            await release_second.wait()
            return ChatResult("second answer", "fake", "fake-model")

        app = ChatTuiApp(
            chat_runner=fake_runner,
            runtime_info=DEEPSEEK_INFO,
            clock=clock,
        )
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("first")
            await pilot.press("enter")
            await asyncio.wait_for(first_started.wait(), timeout=1)

            await pilot.press("escape")
            await asyncio.wait_for(first_cancelled.wait(), timeout=1)

            prompt.load_text("second")
            await pilot.press("enter")
            await asyncio.wait_for(second_started.wait(), timeout=1)
            assert app._last_escape_at is None
            activity = app.query_one("#activity-bar", Static)
            current_activity = str(activity.content)
            assert "思考中" in current_activity

            # 模拟已经排队的旧 Timer tick，并让吞掉取消的旧 Worker 返回。
            app._refresh_activity(1)
            assert str(activity.content) == current_activity
            release_first.set()
            await pilot.pause()
            assert "思考中" in str(activity.content)
            assert app._activity_generation == app._request_generation
            assert "late first" not in transcript_text(app)

            release_second.set()
            await app.workers.wait_for_complete()
            assert str(activity.content) == ""
            assert "second answer" in transcript_text(app)

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
            assert "You" in transcript
            assert "hello" in transcript
            assert "Assistant\nanswer" in transcript
            assert "System\n耗时：1.23 秒" in transcript

    asyncio.run(scenario())


def test_up_down_navigates_input_history_and_restores_draft():
    async def scenario():
        async def fake_runner(input_text: str) -> ChatResult:
            return ChatResult(f"answer: {input_text}", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            for text in ("first", "second", "third"):
                prompt.load_text(text)
                await pilot.press("enter")
                await app.workers.wait_for_complete()

            prompt.load_text("未发送草稿")
            await pilot.press("up")
            assert prompt.text == "third"
            assert prompt.cursor_location == prompt.document.end

            await pilot.press("up")
            assert prompt.text == "second"
            await pilot.press("up")
            assert prompt.text == "first"
            await pilot.press("up")
            assert prompt.text == "first"

            await pilot.press("down")
            assert prompt.text == "second"
            await pilot.press("down")
            assert prompt.text == "third"
            await pilot.press("down")
            assert prompt.text == "未发送草稿"
            assert prompt.cursor_location == prompt.document.end
            await pilot.press("down")
            assert prompt.text == "未发送草稿"

    asyncio.run(scenario())


def test_empty_input_history_keeps_current_draft():
    async def scenario():
        async def fake_runner(input_text: str) -> ChatResult:
            raise AssertionError("runner should not be called")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("当前草稿")

            await pilot.press("up")
            await pilot.press("down")

            assert prompt.text == "当前草稿"
            assert app._history_index is None

    asyncio.run(scenario())


def test_input_history_restores_only_user_messages_with_original_text(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        store.save(
            (
                ChatMessage(ChatRole.USER, "中文\n多行"),
                ChatMessage(ChatRole.ASSISTANT, "不应进入输入历史"),
                ChatMessage(ChatRole.USER, "重复输入"),
                ChatMessage(ChatRole.ASSISTANT, "answer 2"),
                ChatMessage(ChatRole.USER, "重复输入"),
                ChatMessage(ChatRole.ASSISTANT, "answer 3"),
            )
        )
        app = ChatTuiApp(
            chat_session=ChatSession.load(store),
            runtime_info=ALIYUN_INFO,
        )

        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            await pilot.press("up")
            assert prompt.text == "重复输入"
            await pilot.press("up")
            assert prompt.text == "重复输入"
            await pilot.press("up")
            assert prompt.text == "中文\n多行"
            assert prompt.cursor_location == prompt.document.end

    asyncio.run(scenario())


def test_failed_input_remains_in_current_process_history():
    async def scenario():
        async def fake_runner(input_text: str) -> ChatResult:
            raise ChatRuntimeError(
                ChatErrorCode.TIMEOUT,
                "Upstream request timed out",
            )

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("失败输入")
            await pilot.press("enter")
            await app.workers.wait_for_complete()

            await pilot.press("up")
            assert prompt.text == "失败输入"

    asyncio.run(scenario())


def test_clear_failure_preserves_input_history(tmp_path, monkeypatch):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        store.save(
            (
                ChatMessage(ChatRole.USER, "保留输入"),
                ChatMessage(ChatRole.ASSISTANT, "answer"),
            )
        )
        session = ChatSession.load(store)

        def fail_clear():
            raise SessionStoreError("Unable to clear saved conversation")

        monkeypatch.setattr(store, "clear", fail_clear)
        app = ChatTuiApp(chat_session=session, runtime_info=ALIYUN_INFO)

        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("/clear")
            await pilot.press("enter")

            prompt.load_text("")
            await pilot.press("up")
            assert prompt.text == "保留输入"
            assert "Unable to clear saved conversation" in transcript_text(app)

    asyncio.run(scenario())


def test_assistant_output_renders_markdown():
    async def scenario():
        markdown_answer = """## 结果

- 第一项

> 中文引用

**重点**与[文档](https://example.com)

| 列 | 值 |
| --- | --- |
| A | B |

```python
print("hello")
```"""

        async def fake_runner(input_text: str) -> ChatResult:
            return ChatResult(markdown_answer, "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("render markdown")

            await pilot.press("enter")
            await app.workers.wait_for_complete()

            transcript = transcript_text(app)
            assert "Assistant" in transcript
            assert "结果" in transcript
            assert "第一项" in transcript
            assert "中文引用" in transcript
            assert "重点" in transcript
            assert "文档" in transcript
            assert "A" in transcript
            assert "B" in transcript
            assert 'print("hello")' in transcript
            assert "## 结果" not in transcript
            assert "> 中文引用" not in transcript
            assert "**重点**" not in transcript
            assert "[文档](" not in transcript
            assert "| --- |" not in transcript
            assert "```python" not in transcript

    asyncio.run(scenario())


def test_restored_assistant_history_renders_markdown(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        store.save(
            (
                ChatMessage(ChatRole.USER, "上次的问题"),
                ChatMessage(ChatRole.ASSISTANT, "**重要回答**"),
            )
        )
        app = ChatTuiApp(
            chat_session=ChatSession.load(store),
            runtime_info=ALIYUN_INFO,
        )

        async with app.run_test():
            transcript = transcript_text(app)
            assert "Assistant" in transcript
            assert "重要回答" in transcript
            assert "**重要回答**" not in transcript
            assert app.chat_session.messages[1].content == "**重要回答**"
            assert SessionStore(store.path).load()[1].content == "**重要回答**"

    asyncio.run(scenario())


def test_non_assistant_messages_keep_markdown_markers_as_plain_text():
    async def scenario():
        async def fake_runner(input_text: str) -> ChatResult:
            return ChatResult("ok", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("# 用户标题 **原文**")

            await pilot.press("enter")
            await app.workers.wait_for_complete()
            app._write_message("System", "## 系统原文")
            app._write_message("Error", "**错误原文**")

            transcript = transcript_text(app)
            assert "# 用户标题 **原文**" in transcript
            assert "## 系统原文" in transcript
            assert "**错误原文**" in transcript

    asyncio.run(scenario())


def test_user_message_uses_background_card_without_styling_other_roles():
    async def scenario():
        async def fake_runner(input_text: str) -> ChatResult:
            return ChatResult("普通回答", "fake", "fake-model")

        app = ChatTuiApp(chat_runner=fake_runner, runtime_info=ALIYUN_INFO)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("用户 **原文**")

            await pilot.press("enter")
            await app.workers.wait_for_complete()

            lines = app.query_one("#transcript", RichLog).lines
            user_line = next(line for line in lines if "用户 **原文**" in line.text)
            user_segment = next(
                segment for segment in user_line if "用户 **原文**" in segment.text
            )
            assistant_line = next(line for line in lines if "普通回答" in line.text)
            assistant_segment = next(
                segment for segment in assistant_line if "普通回答" in segment.text
            )

            assert any("╭" in line.text and "You" in line.text for line in lines)
            assert user_segment.style is not None
            assert user_segment.style.color is not None
            assert user_segment.style.bgcolor is not None
            assert (
                assistant_segment.style is None
                or assistant_segment.style.bgcolor is None
            )
            assert "用户 **原文**" in transcript_text(app)

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
            assert app._input_history == []

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
            assert app._input_history == []
            prompt.load_text("clear 后草稿")
            await pilot.press("up")
            assert prompt.text == "clear 后草稿"

    asyncio.run(scenario())


def test_tui_restores_saved_conversation_on_mount(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        store.save(
            (
                ChatMessage(ChatRole.USER, "上次的问题"),
                ChatMessage(ChatRole.ASSISTANT, "上次的回答"),
            )
        )
        app = ChatTuiApp(
            chat_session=ChatSession.load(store),
            runtime_info=ALIYUN_INFO,
        )

        async with app.run_test():
            transcript = transcript_text(app)
            assert "You" in transcript
            assert "上次的问题" in transcript
            assert "Assistant\n上次的回答" in transcript

    asyncio.run(scenario())


def test_clear_command_removes_saved_conversation(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "chat-session.json")
        store.save(
            (
                ChatMessage(ChatRole.USER, "question"),
                ChatMessage(ChatRole.ASSISTANT, "answer"),
            )
        )
        app = ChatTuiApp(
            chat_session=ChatSession.load(store),
            runtime_info=ALIYUN_INFO,
        )

        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("/clear")
            await pilot.press("enter")

            assert transcript_text(app) == ""
            assert app.chat_session.messages == ()
            assert not store.path.exists()

    asyncio.run(scenario())


def test_corrupt_history_requires_explicit_clear(tmp_path, monkeypatch):
    async def scenario():
        path = tmp_path / "chat-session.json"
        path.write_text("not-json", encoding="utf-8")
        store = SessionStore(path)
        monkeypatch.setattr(tui_application, "SessionStore", lambda: store)
        app = ChatTuiApp(runtime_info=ALIYUN_INFO)

        async with app.run_test() as pilot:
            assert app.run_status is RunStatus.ERROR
            assert "Unable to load saved conversation" in transcript_text(app)

            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("must not overwrite")
            await pilot.press("enter")
            assert path.read_text(encoding="utf-8") == "not-json"

            prompt.load_text("/clear")
            await pilot.press("enter")
            assert not path.exists()
            assert transcript_text(app) == ""
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

            prompt.load_text("")
            await pilot.press("up")
            assert prompt.text == "first"
            await pilot.press("down")
            assert prompt.text == ""

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
        clock = ManualClock(10.0)

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
            clock=clock,
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

            clock.advance(0.2)
            await pilot.press("escape")
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            assert not exit_called
            assert str(app.query_one("#activity-bar", Static).content) == ""
            assert app._activity_timer is None
            assert "System\n再次按 Esc 退出" in transcript_text(app)
            assert "耗时：" not in transcript_text(app)

            prompt.load_text("")
            await pilot.press("up")
            assert prompt.text == "hello"

            clock.advance(0.1)
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
        assert app._input_history == []

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
        assert app._activity_timer is None

    asyncio.run(scenario())
