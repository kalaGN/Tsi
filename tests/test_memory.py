import json
from datetime import datetime, timezone

import pytest

from app.runtime.memory import (
    ConversationState,
    ConversationSummary,
    UserPreference,
    build_memory_prompt,
    estimate_messages_tokens,
    estimate_text_tokens,
    extract_explicit_preferences,
    parse_conversation_summary,
    resolve_memory_policy,
)
from app.services.llm.contracts import ChatMessage, ChatRole


SUMMARY = ConversationSummary(
    "完成上下文压缩", ("使用双边界",), ("保留完整历史",),
    ("完成设计",), ("补测试",), ("docs/plan/a.md",), (),
)


def test_token_estimator_handles_ascii_non_ascii_and_message_overhead():
    assert estimate_text_tokens("abcdefgh") == 2
    assert estimate_text_tokens("中文") == 2
    assert estimate_messages_tokens((ChatMessage(ChatRole.USER, "abcd"),)) == 5


def test_legacy_memory_policy_environment_remains_validated_for_compatibility():
    assert resolve_memory_policy({}).context_window_tokens == 128_000
    assert resolve_memory_policy({"TUI_CONTEXT_WINDOW_TOKENS": "64000"}).context_window_tokens == 64_000
    with pytest.raises(ValueError, match="positive integer"):
        resolve_memory_policy({"TUI_CONTEXT_WINDOW_TOKENS": "invalid"})


def test_extracts_only_explicit_non_sensitive_preferences_and_deduplicates():
    now = datetime(2026, 9, 9, tzinfo=timezone.utc)
    preferences = extract_explicit_preferences(
        "请记住使用中文回复。以后请优先写必要注释。请记住 API_KEY=secret。请记住 123456",
        (), now=now,
    )
    repeated = extract_explicit_preferences("我的偏好是使用中文回复", preferences, now=now)
    assert [item.content for item in preferences] == ["使用中文回复", "优先写必要注释"]
    assert [item.content for item in repeated].count("使用中文回复") == 1


def test_memory_prompt_marks_summary_preferences_and_omitted_gap_as_untrusted():
    preference = UserPreference("0" * 16, "使用中文回复", "explicit", "2026-09-09T00:00:00Z")
    prompt = build_memory_prompt(SUMMARY, (preference,), omitted_turns=3)
    assert "低优先级" in prompt
    assert "使用中文回复" in prompt
    assert '"omitted_turns":3' in prompt
    assert "不要猜测" in prompt


def test_structured_summary_round_trips_canonical_json():
    parsed = parse_conversation_summary(SUMMARY.to_json())
    assert parsed == SUMMARY
    assert json.loads(parsed.to_json())["pending"] == ["补测试"]


@pytest.mark.parametrize("raw", [
    "```json\n{}\n```",
    '{}',
    '{"goal":"a","goal":"b","decisions":[],"constraints":[],"completed":[],"pending":[],"references":[],"uncertainties":[]}',
    '{"goal":"","decisions":[],"constraints":[],"completed":[],"pending":[],"references":[],"uncertainties":[]}',
    '{"goal":"a","decisions":[],"constraints":[],"completed":[],"pending":[],"references":[],"uncertainties":[],"extra":[]}',
])
def test_structured_summary_rejects_fences_missing_duplicate_empty_or_extra_fields(raw):
    with pytest.raises(ValueError):
        parse_conversation_summary(raw)


def test_structured_summary_enforces_item_count_length_raw_and_token_limits():
    payload = SUMMARY.to_payload()
    payload["pending"] = [str(index) for index in range(13)]
    with pytest.raises(ValueError):
        parse_conversation_summary(json.dumps(payload, ensure_ascii=False))
    payload = SUMMARY.to_payload()
    payload["pending"] = ["中" * 501]
    with pytest.raises(ValueError):
        parse_conversation_summary(json.dumps(payload, ensure_ascii=False))
    with pytest.raises(ValueError):
        parse_conversation_summary(" " * (32 * 1024 + 1))
    with pytest.raises(ValueError):
        parse_conversation_summary(SUMMARY.to_json(), output_token_limit=1)


def test_conversation_state_defaults_to_no_summary_or_gap():
    state = ConversationState()
    assert state.summary is None
    assert state.summary_through_message_count == 0
    assert state.context_start_message_count == 0
