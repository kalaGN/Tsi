import asyncio
from datetime import datetime, timezone

import pytest

from app.runtime.memory import (
    ConversationState,
    MemoryPolicy,
    UserPreference,
    build_memory_prompt,
    estimate_messages_tokens,
    estimate_text_tokens,
    extract_explicit_preferences,
    prepare_memory,
    resolve_memory_policy,
)
from app.services.llm.contracts import ChatMessage, ChatRole


def _turn(number: int, size: int = 10):
    return (
        ChatMessage(ChatRole.USER, f"问{number}" + "中" * size),
        ChatMessage(ChatRole.ASSISTANT, f"答{number}" + "文" * size),
    )


def test_token_estimator_handles_ascii_non_ascii_and_message_overhead():
    assert estimate_text_tokens("abcdefgh") == 2
    assert estimate_text_tokens("中文") == 2
    assert estimate_messages_tokens((ChatMessage(ChatRole.USER, "abcd"),)) == 5


def test_memory_policy_context_window_is_configurable_and_validated():
    assert resolve_memory_policy({}).context_window_tokens == 128_000
    configured = resolve_memory_policy({"TUI_CONTEXT_WINDOW_TOKENS": "64000"})
    assert configured.context_window_tokens == 64_000
    with pytest.raises(ValueError, match="positive integer"):
        resolve_memory_policy({"TUI_CONTEXT_WINDOW_TOKENS": "invalid"})


def test_extracts_only_explicit_non_sensitive_preferences_and_deduplicates():
    now = datetime(2026, 9, 9, tzinfo=timezone.utc)

    preferences = extract_explicit_preferences(
        "请记住使用中文回复。以后请优先写必要注释。请记住 API_KEY=secret。请记住 123456",
        (),
        now=now,
    )
    repeated = extract_explicit_preferences(
        "我的偏好是使用中文回复",
        preferences,
        now=now,
    )

    assert [item.content for item in preferences] == [
        "使用中文回复",
        "优先写必要注释",
    ]
    assert [item.content for item in repeated].count("使用中文回复") == 1
    assert all(item.updated_at == "2026-09-09T00:00:00Z" for item in repeated)


def test_memory_prompt_marks_persisted_content_as_low_priority():
    preference = UserPreference(
        "0" * 16,
        "使用中文回复",
        "explicit",
        "2026-09-09T00:00:00Z",
    )

    prompt = build_memory_prompt("用户正在开发 Tsi", (preference,))

    assert "低优先级" in prompt
    assert "使用中文回复" in prompt
    assert "用户正在开发 Tsi" in prompt


def test_prepare_memory_summarizes_old_turns_and_keeps_full_recent_context():
    async def scenario():
        messages = sum((_turn(number, 20) for number in range(10)), ())
        state = ConversationState(messages=messages)
        calls = []

        async def summarize(previous, batch):
            calls.append((previous, batch))
            return "旧对话摘要"

        prepared = await prepare_memory(
            state,
            "当前问题",
            None,
            summarize,
            policy=MemoryPolicy(
                context_window_tokens=340,
                trigger_ratio=0.7,
                target_ratio=0.6,
                recent_turns=2,
                reserved_tokens=20,
            ),
        )

        assert calls and calls[0][1] == messages[:16]
        assert prepared.summary == "旧对话摘要"
        assert prepared.summarized_message_count == 16
        assert prepared.context_messages == messages[16:]
        assert state.messages == messages

    asyncio.run(scenario())


def test_prepare_memory_falls_back_to_pairwise_eviction_when_summary_fails():
    async def scenario():
        messages = sum((_turn(number, 30) for number in range(8)), ())

        async def fail(_previous, _batch):
            raise RuntimeError("upstream detail")

        prepared = await prepare_memory(
            ConversationState(messages=messages),
            "继续",
            None,
            fail,
            policy=MemoryPolicy(
                context_window_tokens=220,
                trigger_ratio=0.7,
                target_ratio=0.5,
                recent_turns=2,
                reserved_tokens=20,
            ),
        )

        assert prepared.summary is None
        assert prepared.summarized_message_count % 2 == 0
        assert prepared.summarized_message_count > 0
        assert prepared.context_messages == messages[prepared.summarized_message_count :]

    asyncio.run(scenario())


def test_prepare_memory_rejects_current_input_that_cannot_fit_hard_budget():
    async def scenario():
        async def summarize(_previous, _batch):
            return "unused"

        with pytest.raises(ValueError, match="context window"):
            await prepare_memory(
                ConversationState(),
                "中" * 100,
                None,
                summarize,
                policy=MemoryPolicy(
                    context_window_tokens=80,
                    trigger_ratio=0.7,
                    target_ratio=0.5,
                    recent_turns=0,
                    reserved_tokens=10,
                ),
            )

    asyncio.run(scenario())
