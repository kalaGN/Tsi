"""通过真实事件记录验证关联隔离、唯一终态与页面诊断安全边界。"""

import asyncio
import io
import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from app.observability.model_logging import LOGGER_NAME, _ModelEventJsonFormatter
from app.observability.request_context import AgentRequest, correlation_fields, request_span
from app.webui.diagnostics import DiagnosticLimiter, validate_diagnostic
from test_webui import create_service, settings_client, WebProvider, PreflightWebProvider


@pytest.fixture
def events():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_ModelEventJsonFormatter())
    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(handler)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    logger.removeHandler(handler)
    handler.close()


def test_context_isolation_and_single_terminal(events, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("app.observability.request_context.time.monotonic", lambda: clock[0])
    request = AgentRequest(entrypoint="web", session_id="a" * 32).bind()
    try:
        request.phase("plan_confirmation")
        clock[0] = 2
        request.phase("model_call")
        clock[0] = 3
        with request_span(call_id="tool-1"):
            assert correlation_fields()["parent_span_id"] == request.context.span_id
            assert correlation_fields()["call_id"] == "tool-1"
        assert "call_id" not in correlation_fields()
        request.finish("completed")
        request.finish("failed")
    finally:
        request.close()
    assert correlation_fields() == {}
    terminal = [event for event in events() if event["event"] == "agent_request_finished"]
    assert len(terminal) == 1
    assert terminal[0]["wait_ms"] == 2000
    assert terminal[0]["execution_ms"] == 1000


def test_concurrent_requests_do_not_share_context(events):
    async def scenario():
        async def run(identifier):
            lifecycle = AgentRequest(entrypoint="web", request_id=identifier, session_id=identifier).bind()
            try:
                await asyncio.sleep(0)
                assert correlation_fields()["session_id"] == identifier
                lifecycle.finish("completed")
            finally:
                lifecycle.close()
        await asyncio.gather(run("a" * 32), run("b" * 32))
    asyncio.run(scenario())
    assert correlation_fields() == {}
    assert len([e for e in events() if e["event"] == "agent_request_finished"]) == 2


def test_web_preflight_and_execution_share_request_id(tmp_path, events):
    class CapturingProvider(WebProvider):
        ids = []
        def create_turn(self, messages, tools, *, request_id, **budget):
            self.ids.append(request_id)
            return super().create_turn(messages, tools, request_id=request_id, **budget)
    provider = CapturingProvider()
    service = create_service(tmp_path, provider)
    service.preflight_enabled = True
    async def scenario():
        return [event async for event in service.stream_message("你好")]
    output = asyncio.run(scenario())
    request_id = output[0]["request_id"]
    assert provider.ids == [request_id, request_id]
    records = events()
    assert all(e["request_id"] == request_id for e in records)
    assert all(e.get("session_id") == service.catalog.current.id for e in records)
    assert sum(e["event"] == "agent_request_finished" for e in records) == 1
    assert records[-1]["outcome"] == "completed"


def test_disconnected_stream_records_unique_terminal(tmp_path, events):
    class WaitingProvider(WebProvider):
        def create_turn(self, messages, tools, *, request_id, **budget):
            turn = super().create_turn(messages, tools, request_id=request_id, **budget)
            async def wait(*args, **kwargs):
                await asyncio.Event().wait()
            turn.next = wait
            return turn
    service = create_service(tmp_path, WaitingProvider())
    async def scenario():
        stream = service.stream_message("你好")
        assert (await anext(stream))["type"] == "request_started"
        await stream.aclose()
    asyncio.run(scenario())
    terminal = [e for e in events() if e["event"] == "agent_request_finished"]
    assert len(terminal) == 1
    assert terminal[0]["outcome"] == "disconnected"


def test_web_internal_failure_has_terminal_event_and_log(tmp_path, events):
    service = create_service(tmp_path)
    async def fail(*args, **kwargs):
        raise RuntimeError("private error detail")
    service.session.send = fail
    async def scenario():
        return [event async for event in service.stream_message("你好")]
    output = asyncio.run(scenario())
    assert output[-1]["type"] == "failed"
    totals = service.statistics_payload()["totals"]
    assert totals["requests"] == 1
    assert totals["failed"] == 1
    assert totals["completed"] == 0
    terminal = [e for e in events() if e["event"] == "agent_request_finished"]
    assert len(terminal) == 1
    assert terminal[0]["outcome"] == "failed"
    assert terminal[0]["error_code"] == "internal"
    assert "private error detail" not in json.dumps(terminal)


def test_completion_payload_failure_is_not_logged_as_success(tmp_path, events):
    service = create_service(tmp_path)
    def fail(*args):
        raise RuntimeError("private completion detail")
    service._context_percent = fail
    async def scenario():
        return [event async for event in service.stream_message("你好")]
    output = asyncio.run(scenario())
    assert output[-1]["type"] == "failed"
    totals = service.statistics_payload()["totals"]
    assert totals["requests"] == 1
    assert totals["failed"] == 1
    assert totals["completed"] == 0
    terminal = [e for e in events() if e["event"] == "agent_request_finished"]
    assert len(terminal) == 1
    assert terminal[0]["outcome"] == "failed"


def test_web_cancel_is_not_misclassified_as_disconnect(tmp_path, events):
    service = create_service(tmp_path)
    async def wait(*args, **kwargs):
        await asyncio.Event().wait()
    service.session.send = wait
    async def scenario():
        stream = service.stream_message("你好")
        assert (await anext(stream))["type"] == "request_started"
        assert service.cancel_current()
        assert (await anext(stream))["type"] == "cancelled"
        await stream.aclose()
    asyncio.run(scenario())
    terminal = [e for e in events() if e["event"] == "agent_request_finished"]
    assert len(terminal) == 1 and terminal[0]["outcome"] == "cancelled"


def test_rejected_plan_logs_wait_phase_and_cancelled(tmp_path, events):
    service = create_service(tmp_path, PreflightWebProvider(json.dumps({
        "kind": "planned", "reason": "跨模块任务",
        "steps": [{"action": "检查代码", "deliverable": "完成代码检查"}], "question": None,
    })))
    service.preflight_enabled = True
    async def reject(*args, **kwargs):
        await asyncio.sleep(0.001)
        return False
    service._approvals.request_plan = reject
    async def scenario():
        return [event async for event in service.stream_message("实现一个功能")]
    output = asyncio.run(scenario())
    assert output[-1]["type"] == "preflight_cancelled"
    terminal = [e for e in events() if e["event"] == "agent_request_finished"]
    assert len(terminal) == 1 and terminal[0]["outcome"] == "cancelled"
    assert terminal[0]["phase"] == "plan_confirmation"
    assert terminal[0]["wait_ms"] > 0


@pytest.mark.parametrize("extra", [{"message": "secret"}, {"stack": "secret"},
    {"error_type": "password"}, {"request_id": "secret-key"}, {"elapsed_ms": float("nan")}])
def test_diagnostics_reject_unbounded_or_private_data(extra):
    with pytest.raises(ValueError):
        validate_diagnostic({"event": "script_error", **extra})


def test_diagnostic_endpoint_security_size_and_rate(tmp_path, monkeypatch):
    recorded = []
    monkeypatch.setattr("app.webui.router.write_diagnostic", recorded.append)
    client, _ = settings_client(tmp_path)
    payload = {"event": "terminal_state_stuck", "request_id": "a" * 32, "page_version": "1"}
    assert client.post("/ui/api/diagnostics", json=payload, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/ui/api/diagnostics", content="x" * 2049, headers={"Content-Type": "application/json"}).status_code == 413
    assert client.post("/ui/api/diagnostics", json={**payload, "message": "secret"}).status_code == 422
    assert client.post("/ui/api/diagnostics", json=payload).status_code == 204
    assert recorded[0]["event"] == "terminal_state_stuck"
    for _ in range(60):
        last = client.post("/ui/api/diagnostics", json=payload)
    assert last.status_code == 429


def test_diagnostic_limiter_has_bounded_window():
    clock = [0]
    limiter = DiagnosticLimiter(limit=2, clock=lambda: clock[0])
    assert limiter.allow() and limiter.allow() and not limiter.allow()
    clock[0] = 60
    assert limiter.allow()


def test_frontend_diagnostics_and_event_stream_protocol():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node 未安装，无法执行页面诊断单元测试")
    script = Path(__file__).with_name("web_diagnostics_test.cjs")
    subprocess.run([node, str(script)], check=True, capture_output=True, text=True)
