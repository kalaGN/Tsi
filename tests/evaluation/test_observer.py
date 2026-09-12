import json
from pathlib import Path

from app.evaluation.contracts import HarnessFingerprint
from app.evaluation.observer import TraceCollector, sanitize_value
from app.runtime.trace import RequestCompletedTraceEvent, RequestStartedTraceEvent, ToolCallStartedTraceEvent, ToolCallCompletedTraceEvent
from app.services.llm.contracts import ChatMessage, ChatRole


def _fingerprint():
    return HarnessFingerprint(None, False, None, "a", "b", "c")


def test_sanitize_value_redacts_secrets_paths_and_long_text(tmp_path):
    value = sanitize_value(
        {
            "api_key": "secret-value",
            "text": f"Authorization: Bearer abc123 {tmp_path}/file.txt",
        },
        workspace_root=tmp_path,
    )

    assert value["api_key"] == "[REDACTED]"
    assert "abc123" not in value["text"]
    assert str(tmp_path) not in value["text"]


def test_collector_builds_sanitized_tool_trace(tmp_path):
    ticks = iter([1.0, 1.1])
    times = iter(["2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.100Z"])
    collector = TraceCollector(
        run_id="run",
        case_id="case",
        trial=1,
        mode="replay",
        provider="replay",
        model="scripted-v1",
        fingerprint=_fingerprint(),
        workspace_root=tmp_path,
        clock=lambda: next(ticks),
        now=lambda: next(times),
    )
    collector.record(RequestStartedTraceEvent("r", "replay", "scripted-v1", (ChatMessage(ChatRole.USER, "x"),)))
    collector.record(ToolCallStartedTraceEvent("r", 1, "c", "tool", json.dumps({"password": "bad"})))
    collector.record(ToolCallCompletedTraceEvent("r", 1, "c", "tool", "success", 1.0, '{"ok":true}'))
    collector.record(RequestCompletedTraceEvent("r", "done", None))

    trace = collector.finalize()

    assert trace.status == "success"
    assert trace.tool_calls[0].arguments == {"password": "[REDACTED]"}
    assert trace.duration_ms == 100.0
